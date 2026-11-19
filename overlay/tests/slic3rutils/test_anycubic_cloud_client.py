"""Offline native-provider checks; no account, network or printer is accessed."""

import copy
import json
import os
import base64
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "resources/scripts"))
from anycubic_cloud.client import CloudClient, CloudError, HttpsTransport, Session
from anycubic_cloud import protocol
from anycubic_cloud.session_store import SessionStore
from anycubic_cloud.worker import handle_request
from anycubic_cloud.device import public_printer, public_telemetry
from anycubic_cloud.printing import PrintFlow, PrintJobs, build_mapping, validate_materials
from anycubic_cloud._windows_secret import _protect


class Transport:
    def __init__(self, *, put_error=False, claim=None, cleanup_error=False):
        self.calls = []
        self.put_error, self.claim, self.cleanup_error = put_error, claim, cleanup_error

    def request(self, method, url, body, headers, timeout):
        payload = json.loads(body) if isinstance(body, bytes) else None
        self.calls.append((method, url, payload, headers))
        if method == "PUT":
            if self.put_error:
                raise RuntimeError("REMOTE_PRIVATE_DETAIL")
            while body.read(2):
                pass
            return b""
        if url.endswith(protocol.UPLOAD_LOCK_PATH):
            return b'{"code":1,"data":{"id":11,"preSignUrl":"https://storage.example/file?signature=synthetic"}}'
        if url.endswith(protocol.UPLOAD_CLAIM_PATH):
            return self.claim or b'{"code":1,"data":{"id":22}}'
        if url.endswith(protocol.UPLOAD_UNLOCK_PATH):
            if self.cleanup_error:
                raise RuntimeError("REMOTE_PRIVATE_DETAIL")
            return b'{"code":1,"data":{}}'
        return b'{"code":1,"data":[]}'


class CloudClientTests(unittest.TestCase):
    def setUp(self):
        # Keep test data in the writable build/workspace, including in restricted
        # Windows runners whose system TEMP can be unreadable to child processes.
        test_root = (Path.cwd() / ".test/anycubic-cloud").resolve()
        test_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=test_root, prefix="offline-")
        self.assertTrue(Path(self.tmp.name).resolve().is_relative_to(test_root))
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "prepared.gcode"
        self.path.write_bytes(b"; OFFLINE TEST ONLY\n")

    def client(self, **kwargs):
        transport = Transport(**kwargs)
        return CloudClient(session=Session("synthetic-account-secret"), transport=transport), transport

    def test_upload_reserves_registers_and_releases_without_starting_a_print(self):
        client, transport = self.client()
        result = client.upload_gcode(self.path)
        self.assertEqual(result.file_id, 22)
        self.assertFalse(result.cleanup_pending)
        self.assertEqual([call[0] for call in transport.calls], ["POST", "PUT", "POST", "POST"])
        self.assertEqual(transport.calls[-1][2], {"id": 11, "is_delete_cos": 0})
        self.assertNotIn("XX-Token", transport.calls[1][3])
        self.assertFalse(any("sendOrder" in call[1] for call in transport.calls))

    def test_upload_failure_deletes_reservation_and_hides_remote_details(self):
        client, transport = self.client(put_error=True)
        with self.assertRaises(CloudError) as raised:
            client.upload_gcode(self.path)
        self.assertEqual(str(raised.exception), "upload_failed")
        self.assertEqual(transport.calls[-1][2]["is_delete_cos"], 1)

    def test_uncertain_registration_preserves_uploaded_data(self):
        client, transport = self.client(claim=b'{"code":1,"data":{"id":true}}')
        with self.assertRaises(CloudError) as raised:
            client.upload_gcode(self.path)
        self.assertEqual(transport.calls[-1][2]["is_delete_cos"], 0)
        self.assertTrue(raised.exception.cleanup_pending)

    def test_rejected_requests_expose_only_a_bounded_numeric_diagnostic(self):
        client, _ = self.client(claim=b'{"code":-101,"msg":"REMOTE_PRIVATE_DETAIL"}')
        with self.assertRaises(CloudError) as raised:
            client.upload_gcode(self.path)
        self.assertEqual(raised.exception.api_code, -101)
        self.assertNotIn('REMOTE_PRIVATE_DETAIL', str(raised.exception))
        self.assertIsNone(CloudError('api_rejected', api_code=True).api_code)
        self.assertIsNone(CloudError('api_rejected', api_code=2**40).api_code)

    def test_explicitly_rejected_registration_deletes_unclaimed_storage(self):
        client, transport = self.client(claim=b'{"code":0,"msg":"REMOTE_PRIVATE_DETAIL"}')
        with self.assertRaisesRegex(CloudError, "api_rejected") as raised:
            client.upload_gcode(self.path)
        self.assertEqual(transport.calls[-1][2]["is_delete_cos"], 1)
        self.assertFalse(raised.exception.cleanup_pending)

    def test_cleanup_failure_is_reported_separately_from_a_completed_upload(self):
        client, _ = self.client(cleanup_error=True)
        result = client.upload_gcode(self.path)
        self.assertEqual(result.file_id, 22)
        self.assertTrue(result.cleanup_pending)

    def test_cancelled_upload_creates_no_reservation(self):
        client, transport = self.client()
        with self.assertRaisesRegex(CloudError, "cancelled"):
            client.upload_gcode(self.path, cancelled=lambda: True)
        self.assertEqual(transport.calls, [])

    def test_cancellation_after_reservation_still_releases_storage(self):
        client, transport = self.client()
        with self.assertRaisesRegex(CloudError, "cancelled"):
            client.upload_gcode(self.path, cancelled=lambda: bool(transport.calls))
        self.assertEqual(transport.calls[-1][2]["is_delete_cos"], 1)

    def test_wrong_file_type_never_calls_the_cloud(self):
        client, transport = self.client()
        with self.assertRaisesRegex(CloudError, "gcode_required"):
            client.upload_gcode(self.path.with_suffix(".3mf"))
        self.assertEqual(transport.calls, [])

    def test_sign_in_is_required_before_reserving_storage(self):
        transport = Transport()
        with self.assertRaisesRegex(CloudError, "sign_in_required"):
            CloudClient(transport=transport).upload_gcode(self.path)
        self.assertEqual(transport.calls, [])

    def test_empty_file_never_calls_the_cloud(self):
        client, transport = self.client()
        self.path.write_bytes(b"")
        with self.assertRaisesRegex(CloudError, "input_empty"):
            client.upload_gcode(self.path)
        self.assertEqual(transport.calls, [])

    def test_account_secrets_are_not_in_representations(self):
        client, _ = self.client()
        self.assertNotIn("synthetic-account-secret", repr(client))
        self.assertNotIn("synthetic-account-secret", repr(client.session))

    def test_rejected_account_exchange_does_not_replace_the_existing_session(self):
        client, _ = self.client()
        with self.assertRaisesRegex(CloudError, "invalid_response"):
            client.exchange_session("synthetic-import", "synthetic-client")
        self.assertEqual(client.session.token, "synthetic-account-secret")

    def test_printer_list_request_uses_the_account_token(self):
        client, transport = self.client()
        self.assertEqual(client.list_printers(), [])
        self.assertEqual(transport.calls[0][3]["XX-Token"], "synthetic-account-secret")
        self.assertEqual(transport.calls[0][2]["type"], "FDM")

    def test_cleanup_failure_does_not_hide_the_original_upload_failure(self):
        client, _ = self.client(put_error=True, cleanup_error=True)
        with self.assertRaisesRegex(CloudError, "upload_failed") as raised:
            client.upload_gcode(self.path)
        self.assertTrue(raised.exception.cleanup_pending)

    def test_invalid_https_destinations_are_rejected_before_connecting(self):
        for url in ["http://storage.example/file", "https://user@storage.example/file",
                    "https://storage.example:444/file", "https://storage.example/file#fragment"]:
            with self.subTest(url=url), patch("http.client.HTTPSConnection") as connection:
                with self.assertRaises(CloudError):
                    HttpsTransport().request("PUT", url, b"", {}, 1)
                connection.assert_not_called()

    def test_signed_path_is_preserved_and_tls_verification_is_enabled(self):
        with patch("http.client.HTTPSConnection") as factory:
            connection = factory.return_value
            response = connection.getresponse.return_value
            response.status, response.read.return_value = 200, b"ok"
            self.assertEqual(HttpsTransport().request("PUT", "https://storage.example/a%2Fb?q=x%2By", b"x", {}, 1), b"ok")
            context = factory.call_args.kwargs["context"]
            self.assertTrue(context.check_hostname)
            connection.request.assert_called_once_with("PUT", "/a%2Fb?q=x%2By", body=b"x", headers={})
            connection.close.assert_called_once()

    def test_redirect_is_not_followed_and_response_is_not_read(self):
        with patch("http.client.HTTPSConnection") as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 302
            with self.assertRaisesRegex(CloudError, "http_error"):
                HttpsTransport().request("PUT", "https://storage.example/file", b"", {}, 1)
            response.read.assert_not_called()
            self.assertEqual(factory.call_count, 1)


class ConfirmedPrintTests(unittest.TestCase):
    def setUp(self):
        root = (Path.cwd() / '.test/anycubic-cloud').resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root, prefix='print-')
        self.assertTrue(Path(self.tmp.name).resolve().is_relative_to(root))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'offline.gcode'
        self.path.write_bytes(b'; OFFLINE ONLY\nT0\nT3\n')
        self.printer = {'id': 12, 'name': 'Offline test printer', 'model': 'Kobra X',
                        'device_status': 1, 'is_printing': 1, 'multi_color_box': [{'id': -1, 'slots': [
                            {'index': i, 'status': 5, 'edit_status': 1, 'type': 'PLA', 'color': [i, 2, 3, 255]}
                            for i in range(4)]}]}
        self.client = Mock()
        self.client.session = Session('synthetic-only-secret')
        self.client.list_printers.side_effect = lambda: [copy.deepcopy(self.printer)]
        self.client.upload_gcode.return_value = Mock(file_id=22, size=20, cleanup_pending=False)
        self.jobs = PrintJobs(self.root/'jobs')
        self.now = 1000
        self.flow = PrintFlow(self.client, self.jobs, clock=lambda: self.now)
        self.arguments = {'printer_id': 12, 'path': str(self.path), 'materials': [
            {'paint_index': i, 'material_type': 'PLA', 'paint_color': '#112233', 'filament_used': 1.5}
            for i in (0, 3)], 'slots': [{'paint_index': 0, 'slot': 3}, {'paint_index': 3, 'slot': 0}]}

    def prepare(self):
        return self.flow.prepare(self.arguments)

    def start_args(self, plan):
        return {'printer_id': 12, 'ticket': plan['ticket'], 'confirmed': True}

    def test_prepare_uploads_a_frozen_file_without_sending_any_print_command(self):
        def uploaded(path, **kwargs):
            self.path.write_bytes(b'CHANGED ORIGINAL')
            self.assertEqual(Path(path).read_bytes(), b'; OFFLINE ONLY\nT0\nT3\n')
            return Mock(file_id=22, size=20, cleanup_pending=False)
        self.client.upload_gcode.side_effect = uploaded
        plan = self.prepare()
        self.client._api.assert_not_called()
        self.assertNotIn('synthetic-only-secret', json.dumps(plan))
        self.assertEqual([(m['paint_index'], m['ams_index']) for m in plan['mapping']], [(0, 3), (3, 0)])
        self.assertEqual(plan['mapping'][0]['ams_color'], [3, 2, 3])
        self.assertEqual(plan['mapping'][0]['paint_color'], [17, 34, 51])
        self.assertFalse(list((self.root/'jobs/12').glob('upload-*')))

    def test_confirmed_ticket_submits_exactly_one_order_and_is_committed_first(self):
        plan = self.prepare()
        self.client._api.side_effect = lambda *args: self.assertEqual(self.jobs.load(12)['state'], 'attempted')
        self.assertEqual(self.flow.start(self.start_args(plan)), {'accepted': True, 'printer_id': 12})
        body = self.client._api.call_args.args[1]
        self.assertEqual(body['order_id'], 1)
        self.assertEqual(body['data']['file_id'], 22)
        self.assertEqual(body['ams_info']['ams_box_mapping'], plan['mapping'])
        self.assertEqual(body['data']['task_settings'], {'ai_detect': 0, 'camera_timelapse': 0})
        with self.assertRaisesRegex(CloudError, 'print_already_attempted'):
            self.flow.start(self.start_args(plan))
        self.client._api.assert_called_once()

    def test_calibration_combinations_survive_preparation_and_worker_restart(self):
        from itertools import product
        for values in product((False, True), repeat=3):
            with self.subTest(values=values):
                options = dict(zip(('auto_leveling', 'vibration_compensation', 'flow_calibration'), values))
                self.arguments['calibration'] = options
                plan = self.prepare()
                self.client._api.assert_not_called()
                expected = {key: int(value) for key, value in options.items()}
                self.assertEqual(plan['calibration'], expected)
                restarted = PrintFlow(self.client, PrintJobs(self.root/'jobs'), clock=lambda: self.now)
                restarted.start(self.start_args(plan))
                self.assertEqual(self.client._api.call_args.args[1]['data']['task_settings'],
                                 {'ai_detect': 0, 'camera_timelapse': 0, **expected})
                self.client._api.assert_called_once()
                restarted.clear({'printer_id': 12, 'confirmed': True})
                self.client._api.reset_mock()

    def test_calibration_is_frozen_to_the_reviewed_ticket(self):
        self.arguments['calibration'] = {'auto_leveling': True, 'vibration_compensation': False, 'flow_calibration': True}
        plan = self.prepare()
        self.arguments['calibration']['auto_leveling'] = False
        start = self.start_args(plan)
        start['calibration'] = {'auto_leveling': False, 'vibration_compensation': True, 'flow_calibration': False}
        self.flow.start(start)
        sent = self.client._api.call_args.args[1]['data']['task_settings']
        self.assertEqual(sent['auto_leveling'], 1)
        self.assertEqual(sent['vibration_compensation'], 0)
        self.assertEqual(sent['flow_calibration'], 1)

    def test_invalid_calibration_is_rejected_before_upload_or_printer_access(self):
        valid = {'auto_leveling': True, 'vibration_compensation': False, 'flow_calibration': False}
        for value in ({}, [], True, {**valid, 'unknown': True}, {**valid, 'auto_leveling': 1},
                      {**valid, 'flow_calibration': 'true'}):
            with self.subTest(value=value):
                self.arguments['calibration'] = value
                with self.assertRaisesRegex(CloudError, 'invalid_request'):
                    self.prepare()
        self.client.list_printers.assert_not_called()
        self.client.upload_gcode.assert_not_called()
        self.client._api.assert_not_called()

    def test_invalid_persisted_calibration_never_sends_a_start(self):
        plan = self.prepare()
        job = self.jobs.load(12)
        for options in ({'arbitrary_command': 1}, [], False, None, ''):
            with self.subTest(options=options):
                job['calibration'] = options
                self.jobs.save(job)
                with self.assertRaisesRegex(CloudError, 'invalid_request'):
                    self.flow.start(self.start_args(plan))
                self.client._api.assert_not_called()
                self.assertEqual(self.jobs.load(12)['state'], 'uploaded')

    def test_legacy_prepared_ticket_keeps_its_original_settings(self):
        plan = self.prepare()
        job = self.jobs.load(12)
        del job['calibration']
        self.jobs.save(job)
        self.flow.start(self.start_args(plan))
        self.assertEqual(self.client._api.call_args.args[1]['data']['task_settings'],
                         {'ai_detect': 0, 'camera_timelapse': 0})

    def test_missing_confirmation_and_invalid_tickets_never_send_an_order(self):
        with self.assertRaisesRegex(CloudError, 'confirmation_required'):
            self.flow.start({'printer_id': 12})
        with self.assertRaisesRegex(CloudError, 'invalid_print_ticket'):
            self.flow.start({'printer_id': 12, 'ticket': 'bad', 'confirmed': True})
        self.client._api.assert_not_called()

    def test_unknown_or_busy_state_blocks_before_upload(self):
        for value in (0, None, True, False, 2, '2', 'free'):
            with self.subTest(value=value):
                self.printer['is_printing'] = value
                with self.assertRaisesRegex(CloudError, 'printer_busy_or_unknown'):
                    self.prepare()
        self.client.upload_gcode.assert_not_called()
        self.client._api.assert_not_called()

    def test_offline_printer_and_other_hardware_are_rejected(self):
        self.printer['device_status'] = 2
        with self.assertRaisesRegex(CloudError, 'printer_offline'):
            self.prepare()
        self.printer.update(device_status=1, model='Kobra S1', name='Kobra X')
        with self.assertRaisesRegex(CloudError, 'unsupported_printer'):
            self.prepare()
        self.client.upload_gcode.assert_not_called()

    def test_mapping_is_rechecked_after_upload_and_before_start(self):
        plan = self.prepare()
        self.printer['multi_color_box'][0]['slots'][3]['color'] = [8, 8, 8]
        with self.assertRaisesRegex(CloudError, 'mapping_changed'):
            self.flow.start(self.start_args(plan))
        self.client._api.assert_not_called()
        self.client.upload_gcode.side_effect = lambda *args, **kwargs: (
            self.printer['multi_color_box'][0]['slots'][3].update(color=[9, 9, 9]) or
            Mock(file_id=23, cleanup_pending=False))
        with self.assertRaisesRegex(CloudError, 'mapping_changed'):
            self.prepare()
        self.client._api.assert_not_called()

    def test_busy_printer_cannot_be_started_after_preparation(self):
        plan = self.prepare()
        self.printer['is_printing'] = 2
        with self.assertRaisesRegex(CloudError, 'printer_busy_or_unknown'):
            self.flow.start(self.start_args(plan))
        self.client._api.assert_not_called()

    def test_empty_mismatched_duplicated_or_external_slots_are_rejected(self):
        slots = self.printer['multi_color_box'][0]['slots']
        for change, expected in [({'edit_status': 2}, 'slot_unavailable'),
                                 ({'type': 'PETG'}, 'material_mismatch'),
                                 ({'color': [True, 0, 0]}, 'slot_color_unknown')]:
            old = copy.deepcopy(slots[3])
            slots[3].update(change)
            with self.assertRaisesRegex(CloudError, expected):
                self.prepare()
            slots[3] = old
        slots.append(copy.deepcopy(slots[3]))
        with self.assertRaisesRegex(CloudError, 'slot_unavailable'):
            self.prepare()
        slots.pop()
        self.printer['multi_color_box'][0]['id'] = 0
        with self.assertRaisesRegex(CloudError, 'unsupported_feeder'):
            self.prepare()
        self.client.upload_gcode.assert_not_called()

    def test_incomplete_duplicate_and_shared_slot_mapping_are_rejected(self):
        for selection in [[], [{'paint_index': 0, 'slot': 3}]*2,
                          [{'paint_index': 0, 'slot': 0}, {'paint_index': 3, 'slot': 0}],
                          [{'paint_index': 0, 'slot': True}, {'paint_index': 3, 'slot': 3}]]:
            self.arguments['slots'] = selection
            with self.assertRaisesRegex(CloudError, 'mapping_required'):
                self.prepare()
        self.client.upload_gcode.assert_not_called()

    def test_changed_account_and_expired_ticket_require_new_preparation(self):
        plan = self.prepare()
        self.client.session = Session('different-synthetic-account')
        with self.assertRaisesRegex(CloudError, 'print_account_changed'):
            self.flow.start(self.start_args(plan))
        self.client.session = Session('synthetic-only-secret')
        self.now += 601
        with self.assertRaisesRegex(CloudError, 'print_ticket_expired'):
            self.flow.start(self.start_args(plan))
        self.client._api.assert_not_called()

    def test_lost_response_is_durable_and_never_automatically_retried(self):
        plan = self.prepare()
        self.client._api.side_effect = CloudError('connection_failed')
        with self.assertRaisesRegex(CloudError, 'start_outcome_unknown'):
            self.flow.start(self.start_args(plan))
        restarted = PrintFlow(self.client, PrintJobs(self.root/'jobs'), clock=lambda: self.now)
        with self.assertRaisesRegex(CloudError, 'print_already_attempted'):
            restarted.start(self.start_args(plan))
        with self.assertRaisesRegex(CloudError, 'print_locked'):
            restarted.prepare(self.arguments)
        self.client._api.assert_called_once()

    def test_explicit_rejection_is_distinguished_from_unknown_response(self):
        plan = self.prepare()
        self.client._api.side_effect = CloudError('api_rejected')
        with self.assertRaisesRegex(CloudError, 'api_rejected'):
            self.flow.start(self.start_args(plan))
        self.assertEqual(self.jobs.load(12)['state'], 'rejected')
        self.prepare()
        self.client._api.assert_called_once()

    def test_busy_then_idle_observation_completes_previous_job(self):
        plan = self.prepare()
        self.flow.start(self.start_args(plan))
        self.printer['is_printing'] = 2
        self.jobs.observe([self.printer])
        self.printer['is_printing'] = 1
        self.jobs.observe([self.printer])
        self.assertEqual(self.jobs.load(12)['state'], 'completed')
        self.prepare()
        self.client._api.assert_called_once()

    def test_clearing_print_protection_requires_confirmation_and_idle_printer(self):
        plan = self.prepare()
        self.flow.start(self.start_args(plan))
        with self.assertRaisesRegex(CloudError, 'confirmation_required'):
            self.flow.clear({'printer_id': 12})
        self.printer['is_printing'] = 2
        with self.assertRaisesRegex(CloudError, 'printer_busy_or_unknown'):
            self.flow.clear({'printer_id': 12, 'confirmed': True})
        self.printer['is_printing'] = 1
        self.flow.clear({'printer_id': 12, 'confirmed': True})
        self.prepare()
        self.client._api.assert_called_once()

    def test_failed_attempt_commit_prevents_sending_the_command(self):
        plan = self.prepare()
        with patch.object(self.jobs, 'save', side_effect=OSError('synthetic disk failure')):
            with self.assertRaisesRegex(CloudError, 'print_state_unreadable'):
                self.flow.start(self.start_args(plan))
        self.client._api.assert_not_called()

    def test_failed_acknowledgement_commit_keeps_the_attempt_locked(self):
        plan = self.prepare()
        original = self.jobs.save
        def save(job):
            if job['state'] == 'accepted':
                raise OSError('synthetic disk failure')
            original(job)
        with patch.object(self.jobs, 'save', side_effect=save):
            with self.assertRaisesRegex(CloudError, 'start_outcome_unknown'):
                self.flow.start(self.start_args(plan))
        self.assertEqual(self.jobs.load(12)['state'], 'attempted')
        with self.assertRaisesRegex(CloudError, 'print_locked'):
            self.prepare()
        self.client._api.assert_called_once()

    def test_corrupted_saved_protection_fails_closed(self):
        plan = self.prepare()
        (self.root/'jobs/12/current.json').write_text('{"schema":false}')
        with self.assertRaisesRegex(CloudError, 'print_state_unreadable'):
            self.flow.start(self.start_args(plan))
        self.client._api.assert_not_called()

    def test_file_lock_prevents_overlapping_workers(self):
        with self.jobs.lock(12):
            with self.assertRaisesRegex(CloudError, 'print_in_progress'):
                with self.jobs.lock(12):
                    self.fail('Second worker acquired the printer lock')

    def test_cancelled_preparation_sends_no_upload_or_order(self):
        with self.assertRaisesRegex(CloudError, 'cancelled'):
            self.flow.prepare(self.arguments, cancelled=lambda: True)
        self.client.upload_gcode.assert_not_called()
        self.client._api.assert_not_called()

    def test_nonfinite_and_unsupported_tools_are_rejected(self):
        for change in [{'paint_index': True}, {'paint_index': 4}, {'filament_used': float('nan')},
                       {'filament_used': -1}, {'paint_color': '#BAD'}, {'material_type': 'PLA\nG28'}]:
            with self.subTest(change=change):
                materials = copy.deepcopy(self.arguments['materials'])
                materials[0].update(change)
                with self.assertRaises(CloudError):
                    validate_materials(materials)


class DeviceMetadataTests(unittest.TestCase):
    def test_nested_installed_firmware_is_exposed_without_update_secrets(self):
        result = public_printer({'firmware_version': '', 'version': {
            'firmware_version': '2.0.2.2', 'target_version': '9.9.9',
            'download_url': 'synthetic-private-url', 'token': 'synthetic-private-token'}})
        self.assertEqual(result['firmware_version'], '2.0.2.2')
        self.assertNotIn('synthetic-private', json.dumps(result))
        self.assertNotIn('9.9.9', json.dumps(result))

    def test_top_level_installed_firmware_takes_precedence(self):
        result = public_printer({'firmware_version': '2.0.2.3',
                                 'version': {'firmware_version': '2.0.2.2'}})
        self.assertEqual(result['firmware_version'], '2.0.2.3')

    def test_malformed_firmware_object_is_not_rendered_as_a_version(self):
        for value in (None, True, 12, ['2.0.2.2'], {'target_version': '9.9.9'},
                      {'firmware_version': {'private': 'data'}}):
            with self.subTest(value=value):
                self.assertNotIn('firmware_version', public_printer({'version': value}))

    def test_missing_feeder_data_remains_unknown(self):
        self.assertNotIn('boxes', public_printer({'id': 1}))
        self.assertEqual(public_printer({'multi_color_box': []})['boxes'], [])

    def test_feeder_slots_are_bounded_and_private_fields_removed(self):
        record = {'name': 'Printer\nTest', 'version': '2.0.2.2', 'token': 'synthetic-secret',
                  'multi_color_box': [{'id': -1, 'secret': 'synthetic-secret', 'slots': [
                      {'index': 0, 'status': 5, 'type': 'PLA', 'material_name': 'Test'},
                      {'index': 0, 'status': 5, 'type': 'Duplicate'},
                      {'index': True, 'status': 5, 'type': 'Invalid'},
                      {'index': 2, 'status': 0, 'edit_status': 2, 'type': 'Stale'},
                      {'index': 3, 'type': 'Unconfirmed'}]}]}
        result = public_printer(record)
        self.assertEqual(result['name'], 'PrinterTest')
        self.assertEqual(result['firmware_version'], '2.0.2.2')
        self.assertNotIn('synthetic-secret', json.dumps(result))
        slots = result['boxes'][0]['slots']
        self.assertEqual([slot['index'] for slot in slots], [0, 2, 3])
        self.assertEqual(slots[0]['type'], 'PLA')
        self.assertIs(slots[1]['present'], False)
        self.assertEqual(slots[1]['type'], '')
        self.assertIsNone(slots[2]['present'])

    def test_malformed_collections_do_not_break_device_listing(self):
        result = public_printer({'multi_color_box': [None, {'id': True}, {'id': -1, 'slots': 4},
                                                   {'id': -1, 'slots': []}, {'id': 50}]})
        self.assertEqual(result['boxes'], [{'id': -1, 'slots': []}])

    def test_stale_material_does_not_imply_a_present_spool(self):
        result = public_printer({'name': 12, 'firmware_version': False, 'multi_color_box': [
            {'id': 0, 'slots': [{'index': 0, 'status': 5, 'edit_status': 2, 'type': 'PLA'},
                                {'index': 1, 'status': 0, 'type': 'PLA'},
                                {'index': 2, 'status': 0, 'edit_status': 1, 'type': 'PETG'}]}]})
        self.assertNotIn('name', result)
        self.assertNotIn('firmware_version', result)
        slots = result['boxes'][0]['slots']
        self.assertIs(slots[0]['present'], False)
        self.assertEqual(slots[0]['type'], '')
        self.assertIsNone(slots[1]['present'])
        self.assertIs(slots[2]['present'], True)


class CloudReadingsTests(unittest.TestCase):
    def printer(self, **changes):
        return {'id': 12, 'device_status': 1, 'is_printing': 2,
                'parameter': {'curr_nozzle_temp': '211.5', 'curr_hotbed_temp': 60}, **changes}

    def project(self, **changes):
        return {'id': 30, 'printer_id': '12', 'print_status': 1, 'gcode_name': 'fixture.gcode',
                'progress': 42, 'remain_time': 66,
                'settings': '{"curr_layer":25,"total_layers":100}', **changes}

    def test_partial_temperatures_preserve_zero_and_do_not_invent_a_missing_value(self):
        result = public_telemetry(self.printer(parameter={'curr_nozzle_temp': 0}), [])
        self.assertEqual(result['nozzle'], 0)
        self.assertNotIn('bed', result)
        self.assertNotIn('job', result)

    def test_invalid_temperatures_are_unknown_instead_of_zero_or_nonfinite_json(self):
        for value in (True, -1, 501, 'NaN', float('inf'), {}, 10**200):
            with self.subTest(value_type=type(value).__name__):
                result = public_telemetry(self.printer(parameter={'curr_nozzle_temp': value}), [])
                self.assertNotIn('nozzle', result)
                json.dumps(result, allow_nan=False)

    def test_active_job_includes_units_and_layers_without_forwarding_private_fields(self):
        result = public_telemetry(self.printer(token='PRIVATE', key='PRIVATE'), [self.project(url='PRIVATE')])
        self.assertEqual(result['job'], {'name':'fixture.gcode', 'project_id':30, 'progress':42,
                                        'remaining_minutes':66, 'layer':25, 'layers':100})
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_tasks_for_other_printers_and_finished_jobs_are_never_used(self):
        for project in (self.project(printer_id=99), self.project(print_status=2)):
            self.assertNotIn('job', public_telemetry(self.printer(), [project]))

    def test_idle_offline_and_ambiguous_tasks_do_not_show_historical_active_jobs(self):
        for printer in (self.printer(is_printing=1), self.printer(device_status=0)):
            self.assertNotIn('job', public_telemetry(printer, [self.project()]))
        self.assertNotIn('job', public_telemetry(self.printer(), [self.project(), self.project(id=31)]))

    def test_malformed_settings_keep_task_progress_and_hide_invalid_layers(self):
        for settings in ('not JSON', '[]', '{"curr_layer":101,"total_layers":100}'):
            result = public_telemetry(self.printer(), [self.project(settings=settings)])['job']
            self.assertEqual(result['progress'], 42)
            self.assertNotIn('layer', result)

    def test_progress_and_remaining_are_bounded_without_losing_zero(self):
        job = public_telemetry(self.printer(), [self.project(progress=0, remain_time=0)])['job']
        self.assertEqual(job['progress'], 0)
        self.assertEqual(job['remaining_minutes'], 0)
        job = public_telemetry(self.printer(), [self.project(progress=101, remain_time=-5)])['job']
        self.assertNotIn('progress', job)
        self.assertNotIn('remaining_minutes', job)

    def test_boolean_ids_do_not_match_printers_or_jobs(self):
        self.assertNotIn('job', public_telemetry(self.printer(id=True), [self.project(printer_id=1)]))

    def test_report_time_displays_seconds_and_milliseconds_as_utc_without_using_download_time(self):
        for value in (1700000000, '1700000000', 1700000000000):
            self.assertEqual(public_telemetry(self.printer(last_update_time=value), [])['reported_at'],
                             '2023-11-14T22:13:20Z')
        self.assertEqual(public_telemetry(self.printer(last_update_time=0), [])['reported_at'], '')
        self.assertEqual(public_telemetry(self.printer(), [])['reported_at'], '')

    def test_get_detail_is_read_only_and_validates_the_returned_printer(self):
        transport = Mock()
        transport.request.return_value = b'{"code":1,"data":{"id":12}}'
        client = CloudClient(session=Session('synthetic-secret'), transport=transport)
        self.assertEqual(client.printer_info('12'), {'id':12})
        args = transport.request.call_args.args
        self.assertEqual(args[0], 'GET')
        self.assertTrue(args[1].endswith('/v2/printer/info?id=12'))
        self.assertIsNone(args[2])
        transport.request.return_value = b'{"code":1,"data":{"id":99}}'
        with self.assertRaisesRegex(CloudError, 'invalid_response'):
            client.printer_info(12)

    def test_invalid_detail_ids_never_send_a_request(self):
        transport = Mock()
        client = CloudClient(session=Session('synthetic-secret'), transport=transport)
        for value in (True, 0, -1, '12&other=3', '1'*100, 1.0):
            with self.assertRaisesRegex(CloudError, 'invalid_request'):
                client.printer_info(value)
        transport.request.assert_not_called()

    def snapshot(self, client, wanted=12, cancelled=lambda:False):
        store=Mock()
        store.load.return_value=Session('synthetic-secret')
        request={'method':'workbench_snapshot','session_path':str((Path.cwd()/'.test/unused-session.sec').resolve()),
                 'args':{'printer_id':wanted}}
        return handle_request(request, client_factory=lambda **kwargs:client,
                              store_factory=lambda path:store, cancelled=cancelled)

    def fixture_client(self):
        client=Mock()
        client.list_printers.return_value=[self.printer(name='Fixture printer', multi_color_box=[])]
        client.printer_info.return_value=self.printer()
        client.recent_projects.return_value=[self.project()]
        return client

    def test_workbench_auto_selects_single_printer_and_keeps_readings_out_of_plain_list(self):
        client=self.fixture_client()
        result=self.snapshot(client, None)['printers'][0]
        self.assertEqual(result['telemetry']['nozzle'],211.5)
        self.assertEqual(result['telemetry']['job']['progress'],42)
        self.assertEqual(result['boxes'],[])
        client.upload_gcode.assert_not_called()
        client.send_order.assert_not_called()
        self.assertNotIn('telemetry', public_printer(client.list_printers.return_value[0]))

    def test_missing_selected_printer_never_reads_another_printer(self):
        client=self.fixture_client()
        result=self.snapshot(client, 99)
        self.assertNotIn('telemetry',result['printers'][0])
        client.printer_info.assert_not_called()
        client.recent_projects.assert_not_called()

    def test_detail_and_task_failures_are_separate_and_hide_remote_errors(self):
        client=self.fixture_client()
        client.printer_info.side_effect=CloudError('api_rejected')
        client.recent_projects.side_effect=CloudError('connection_failed')
        result=self.snapshot(client)['printers'][0]['telemetry']
        self.assertFalse(result['detail_loaded'])
        self.assertFalse(result['projects_loaded'])
        self.assertNotIn('job',result)
        self.assertNotIn('api_rejected',json.dumps(result))

    def test_cancelled_snapshot_does_not_continue_detail_or_project_reads(self):
        client=self.fixture_client()
        with self.assertRaisesRegex(CloudError,'cancelled'):
            self.snapshot(client,cancelled=lambda:True)
        client.printer_info.assert_not_called()
        client.recent_projects.assert_not_called()

    def test_idle_snapshot_does_not_query_account_task_history(self):
        client=self.fixture_client()
        client.printer_info.return_value=self.printer(is_printing=1)
        self.assertNotIn('job',self.snapshot(client)['printers'][0]['telemetry'])
        client.recent_projects.assert_not_called()


class LiveReportsTests(unittest.TestCase):
    def setUp(self):
        from anycubic_cloud.monitor import Reports
        self.now = 0
        self.wall = 1700000000
        self.reports = Reports({'id':12,'machine_type':20030,'key':'offline_key'},
                               clock=lambda:self.now, wall=lambda:self.wall)
        self.reports.connection(True, True)
        self.topic = 'anycubic/anycubicCloud/v1/printer/public/20030/offline_key/status/report'

    def send(self, kind='tempature', action='auto', state='done', data=None, **extra):
        self.reports.receive(self.topic, json.dumps({'type':kind,'action':action,'state':state,
                             'data':data or {},**extra}).encode())

    def fields(self):
        return self.reports.snapshot()['fields']

    def test_partial_temperatures_preserve_zero_and_expire_independently(self):
        self.send(data={'curr_nozzle_temp':0,'curr_hotbed_temp':60})
        self.now=50
        self.send(data={'curr_hotbed_temp':61})
        self.now=61
        self.assertEqual(self.fields(),{'bed':61})
        self.now=111
        self.assertEqual(self.fields(),{})
        self.assertEqual(self.reports.snapshot()['state'],'stale')

    def test_connect_and_subscribe_alone_are_not_live_printer_data(self):
        self.assertEqual(self.reports.snapshot()['state'],'waiting')
        self.assertEqual(self.fields(),{})

    def test_wrong_printer_retained_and_duplicate_reports_do_not_change_values(self):
        packet=json.dumps({'type':'tempature','action':'auto','state':'done',
                           'data':{'curr_nozzle_temp':210}}).encode()
        self.reports.receive(self.topic.replace('offline_key','other_key'),packet)
        self.reports.receive(self.topic,packet,retained=True)
        self.reports.receive(self.topic,packet,duplicate=True)
        self.assertEqual(self.fields(),{})

    def test_invalid_numbers_and_unrelated_private_fields_never_leave_the_parser(self):
        self.send(data={'curr_nozzle_temp':'NaN','curr_hotbed_temp':True,'token':'PRIVATE'})
        self.assertEqual(self.fields(),{})
        self.assertNotIn('PRIVATE',json.dumps(self.reports.snapshot()))

    def test_source_timestamp_is_aged_instead_of_reset_to_arrival_time(self):
        self.send(data={'curr_nozzle_temp':210},timestamp=(self.wall-50)*1000)
        self.assertEqual(self.fields()['nozzle'],210)
        self.now=11
        self.assertEqual(self.fields(),{})

    def test_old_future_and_out_of_order_timestamps_do_not_refresh_values(self):
        self.send(data={'curr_nozzle_temp':210},timestamp=self.wall-61)
        self.send(data={'curr_nozzle_temp':211},timestamp=self.wall+31)
        self.assertEqual(self.fields(),{})
        self.send(data={'curr_nozzle_temp':212},timestamp=self.wall)
        self.send(data={'curr_nozzle_temp':99},timestamp=self.wall-1)
        self.assertEqual(self.fields()['nozzle'],212)

    def test_disconnect_and_reconnect_require_new_reports(self):
        self.send(data={'curr_nozzle_temp':210})
        self.reports.connection(False)
        self.assertEqual(self.fields(),{})
        self.reports.connection(True,True)
        self.assertEqual(self.reports.snapshot()['state'],'waiting')

    def test_job_change_clears_previous_names_layers_and_progress(self):
        self.send('print','start','updated',{'taskid':30,'filename':'old.gcode','curr_layer':50,'total_layers':100,'progress':50})
        self.send('print','start','updated',{'taskid':31,'progress':0})
        self.assertEqual(self.fields(),{'is_printing':2,'progress':0})
        self.send('print','start','updated',{'taskid':30,'progress':99})
        self.assertEqual(self.fields()['progress'],0)

    def test_layer_regression_resets_old_job_fields_without_rejecting_same_id(self):
        self.send('print','start','updated',{'taskid':30,'curr_layer':50,'total_layers':100})
        self.send('print','start','updated',{'taskid':30,'curr_layer':0})
        self.assertNotIn('layers',self.fields())
        self.send('print','start','updated',{'taskid':30,'curr_layer':1,'total_layers':150})
        self.assertEqual(self.fields()['layer'],1)
        self.assertEqual(self.fields()['layers'],150)

    def test_idle_report_clears_previous_job_but_keeps_current_temperature(self):
        self.send(data={'curr_nozzle_temp':210})
        self.send('print','start','updated',{'progress':42})
        self.send('status','workReport','free')
        self.assertEqual(self.fields(),{'nozzle':210,'is_printing':1})
        self.assertTrue(self.reports.snapshot()['job_seen'])

    def test_late_finished_report_for_another_job_does_not_end_current_job(self):
        self.send('print','start','updated',{'taskid':31,'progress':42})
        self.send('print','start','finished',{'taskid':30})
        self.assertEqual(self.fields()['progress'],42)

    def test_kobra_x_updates_without_task_id_keep_partial_progress(self):
        self.send('print','start','updated',{'progress':10,'remain_time':66,'curr_layer':23,'total_layers':263})
        self.assertEqual(self.fields()['progress'],10)
        self.assertEqual(self.fields()['remaining_minutes'],66)
        self.send('print','update','updated',{'curr_nozzle_temp':False,'progress':11})
        self.assertEqual(self.fields()['progress'],11)
        self.assertNotIn('nozzle',self.fields())

    def test_oversize_and_invalid_json_reports_are_ignored(self):
        for payload in (b'not json',b'[]',b'x'*65537):
            self.reports.receive(self.topic,payload)
        self.assertEqual(self.fields(),{})

    def test_print_progress_can_carry_a_partial_temperature_report(self):
        self.send('print','start','updated',{'curr_nozzle_temp':211,'progress':10})
        self.assertEqual(self.fields()['nozzle'],211)
        self.assertNotIn('bed',self.fields())

    def test_public_tls_context_keeps_ca_hostname_and_tls_version_checks(self):
        import ssl
        from anycubic_cloud.monitor import tls_context
        context=tls_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
        self.assertGreaterEqual(context.minimum_version,ssl.TLSVersion.TLSv1_2)
        self.assertEqual(len(context.get_ca_certs(binary_form=True)),1)

    def test_monitor_uses_only_subscriptions_and_closes_its_network_loop(self):
        from anycubic_cloud.monitor import run_monitor
        from types import SimpleNamespace
        client=Mock()
        client.list_printers.return_value=[{'id':12,'machine_type':20030,'key':'offline_key'}]
        client.session=Session('synthetic-offline-only','offline@example.invalid')
        calls=[]
        outer=self
        class Connection:
            def username_pw_set(self,*args): calls.append('credentials')
            def tls_set_context(self,ctx): calls.append('tls')
            def reconnect_delay_set(self,**args): pass
            def subscribe(self,filters):
                outer.assertEqual(len(filters),4)
                outer.assertTrue(all(f.endswith('/report') and qos==0 for f,qos in filters))
                self.on_subscribe(self,None,1,[0]*len(filters))
            def connect(self,host,port,keepalive):
                outer.assertEqual((host,port),('mqtt-universe.anycubic.com',8883))
                self.on_connect(self,None,{},0)
                self.on_message(self,None,SimpleNamespace(topic=outer.topic,
                    payload=b'{"type":"tempature","action":"auto","state":"done","data":{"curr_nozzle_temp":210}}',retain=False,dup=False))
            def loop_start(self): calls.append('loop_start')
            def loop_stop(self): calls.append('loop_stop')
            def disconnect(self): calls.append('disconnect')
            def publish(self,*args,**kwargs): raise AssertionError('Printer commands forbidden')
        updates=[]
        with patch('anycubic_cloud.monitor.credentials',return_value=('offline','offline','offline')):
            result=run_monitor(client,12,cancelled=lambda:len(updates)>1,update=updates.append,
                               mqtt_factory=lambda identity:Connection())
        self.assertEqual(updates[-1]['fields']['nozzle'],210)
        self.assertEqual(calls[-2:],['disconnect','loop_stop'])
        self.assertTrue(result['stopped'])

    def test_monitor_cancellation_does_not_create_a_broker_client(self):
        from anycubic_cloud.monitor import run_monitor
        client=Mock()
        client.list_printers.return_value=[{'id':12,'machine_type':20030,'key':'offline_key'}]
        factory=Mock()
        with self.assertRaisesRegex(CloudError,'cancelled'):
            run_monitor(client,12,cancelled=lambda:True,update=lambda value:None,mqtt_factory=factory)
        factory.assert_not_called()


    def test_fan_reports_are_bounded_and_ignore_request_echoes(self):
        self.send('fan','auto','done',{'fan_speed_pct':0,'token':'PRIVATE'})
        self.assertEqual(self.fields(),{'part_fan':0})
        self.send('fan','set','done',{'fan_speed_pct':99})
        self.assertEqual(self.fields()['part_fan'],0)
        self.send('fan','query','done',{'fan_speed_pct':100})
        self.assertEqual(self.fields()['part_fan'],100)
        for value in [True,-1,101,'50',50.5]:self.send('fan','auto','done',{'fan_speed_pct':value})
        self.assertEqual(self.fields()['part_fan'],100)
        self.assertNotIn('PRIVATE',json.dumps(self.reports.snapshot()))

    def test_light_query_and_control_use_only_unambiguous_reported_types(self):
        self.send('light','query','done',{'lights':[{'type':2,'status':1,'token':'PRIVATE'}]})
        self.assertEqual(self.fields(),{'light_type':2,'light_on':True})
        self.send('light','control','done',{'type':2,'status':0})
        self.assertEqual(self.fields()['light_on'],False)
        self.send('light','query','done',{'lights':[{'type':1,'status':0},{'type':2,'status':1}]})
        self.assertNotIn('light_type',self.fields())
        self.assertNotIn('PRIVATE',json.dumps(self.reports.snapshot()))

    def test_unknown_or_malformed_light_reports_cannot_enable_control(self):
        for data in [{'type':-1,'status':1},{'type':True,'status':1},{'type':1.5,'status':True},
                     {'type':2**31,'status':1},{'type':'bad','status':1},
                     {'status':1},{'lights':[]},{'lights':[None]}]:
            self.send('light','query','done',data)
            self.assertNotIn('light_type',self.fields())

    def test_oversized_light_list_does_not_hide_a_second_supported_light(self):
        lights = [{'type':1,'status':0}] + [None]*15 + [{'type':2,'status':1}]
        self.send('light','query','done',{'lights':lights})
        self.assertNotIn('light_type',self.fields())

    def test_subscription_includes_the_dedicated_light_and_fan_report_channels(self):
        channels={topic.rsplit('/',2)[-2] for topic,qos in self.reports.filters()}
        self.assertEqual(channels,{'status','print','light','fan'})
        self.assertTrue(all(qos==0 and topic.endswith('/report') for topic,qos in self.reports.filters()))

    def test_optional_subscription_denial_does_not_disable_core_reports(self):
        from anycubic_cloud.monitor import subscription_state
        self.assertEqual(subscription_state([0,0,128,0]),(True,False,True))
        self.assertEqual(subscription_state([128,0,0,0]),(False,True,True))
        self.assertEqual(subscription_state([0,0]),(False,False,False))
        self.reports.connection(True,True,light_subscribed=False,fan_subscribed=True)
        self.send(data={'curr_nozzle_temp':205})
        self.assertEqual(self.fields()['nozzle'],205)
        self.assertTrue(self.reports.snapshot()['connected'])
        self.assertFalse(self.reports.snapshot()['light_reports_subscribed'])

    def test_light_query_reply_on_its_dedicated_topic_reaches_the_ui_fields(self):
        topic=self.topic.replace('/status/report','/light/report')
        self.reports.receive(topic,b'{"type":"light","action":"query","state":"done","data":{"lights":[{"type":2,"status":1}]}}')
        self.assertEqual(self.fields()['light_type'],2)
        self.assertTrue(self.fields()['light_on'])

    def test_dedicated_light_topic_can_identify_a_reply_without_an_envelope_type(self):
        topic=self.topic.replace('/status/report','/light/report')
        self.reports.receive(topic,b'{"action":"query","state":"done","data":{"type":2,"status":0}}')
        self.assertEqual(self.fields()['light_type'],2)
        self.assertFalse(self.fields()['light_on'])

    def test_numeric_light_strings_are_normalized_without_guessing_a_type(self):
        self.send('light','query','done',{'type':'2','status':'1'})
        self.assertEqual(self.fields()['light_type'],2)
        self.assertTrue(self.fields()['light_on'])
        self.send('light','query','done',{'type':'2','status':'0'})
        self.assertFalse(self.fields()['light_on'])

    def test_reported_light_enum_is_not_restricted_to_one_and_two(self):
        for kind in [0,3,40,2**31-1,'0','40']:
            with self.subTest(kind=kind):
                self.send('light','query','done',{'type':kind,'status':1})
                self.assertEqual(self.fields(),{'light_type':int(kind),'light_on':True})

    def test_missing_or_unknown_state_does_not_discard_explicit_light_identity(self):
        self.send('light','query','done',{'type':0,'status':1})
        for data in [{'type':0},{'type':0,'status':'unknown'},{'type':0,'status':2}]:
            self.send('light','query','done',data)
            self.assertEqual(self.fields(),{'light_type':0})

    def test_boolean_state_and_direct_list_response_preserve_reported_identity(self):
        for status in [True,False]:
            self.send('light','query','done',[{'type':0,'status':status}])
            self.assertEqual(self.fields(),{'light_type':0,'light_on':status})

    def test_multiple_entries_do_not_select_even_one_recognized_light(self):
        for lights in [[{'type':0},{'type':0}], [{'type':0},None]]:
            self.send('light','query','done',{'lights':lights})
            self.assertNotIn('light_type',self.fields())
            self.assertEqual(self.fields()['light_report_problem'],'ambiguous')

    def test_dedicated_fan_report_and_legacy_report_aliases_are_read_only(self):
        topic=self.topic.replace('/status/report','/fan/report')
        self.reports.receive(topic,b'{"type":"fan","action":"reportInfo","state":"success","data":{"fan_speed_pct":50}}')
        self.assertEqual(self.fields()['part_fan'],50)
        self.reports.receive(topic,b'{"type":"print","action":"start","state":"printing","data":{"taskid":123}}')
        self.assertNotIn('control_job',self.reports.snapshot())

    def test_light_problem_diagnostics_are_fixed_identifiers_and_clear_on_valid_reply(self):
        self.send('light','query','done',{'status':1,'PRIVATE':'PRIVATE'})
        self.assertEqual(self.fields()['light_report_problem'],'unsupported')
        self.assertNotIn('PRIVATE',json.dumps(self.reports.snapshot()))
        self.send('light','query','done',{'type':1,'status':1})
        self.assertNotIn('light_report_problem',self.fields())

    def test_control_identity_expires_before_general_readings(self):
        self.send('print','start','printing',{'taskid':30,'progress':40})
        self.assertEqual(self.reports.snapshot()['control_job'],{'project_id':30,'phase':'printing'})
        self.now=11
        self.assertNotIn('control_job',self.reports.snapshot())
        self.assertEqual(self.fields()['progress'],40)

    def test_control_identity_is_not_inferred_from_a_packet_without_taskid(self):
        self.send('print','start','printing',{'taskid':30})
        self.send('print','update','updated',{'progress':50})
        self.assertNotIn('control_job',self.reports.snapshot())

    def test_control_phase_requires_an_explicit_printing_or_paused_report(self):
        self.send('print','pause','paused',{'taskid':30})
        self.assertEqual(self.reports.snapshot()['control_job']['phase'],'paused')
        self.send('print','resume','resuming',{'taskid':30})
        self.assertEqual(self.reports.snapshot()['control_job']['phase'],'unknown')
        self.send('print','resume','resumed',{'taskid':30})
        self.assertEqual(self.reports.snapshot()['control_job']['phase'],'printing')
        self.send('status','workReport','free')
        self.assertNotIn('control_job',self.reports.snapshot())

    def test_light_and_fan_expire_without_refresh_from_unrelated_reports(self):
        self.send('light','query','done',{'type':1,'status':0})
        self.send('fan','auto','done',{'fan_speed_pct':40})
        self.now=61
        self.send(data={'curr_nozzle_temp':200})
        self.assertEqual(self.fields(),{'nozzle':200})

    def test_report_age_increases_without_pretending_old_values_are_live(self):
        self.send(data={'curr_nozzle_temp':200})
        self.now=30
        self.assertEqual(self.reports.snapshot()['report_age_seconds'],30)
        self.now=61
        self.assertNotIn('nozzle',self.fields())
        self.assertNotEqual(self.reports.snapshot()['state'],'live')
        self.reports.connection(False)
        self.assertFalse(self.reports.snapshot()['connected'])
        self.reports.connection(True,True)
        self.assertEqual(self.fields(),{})


@unittest.skipUnless(os.name == 'nt', 'Windows CNG public-key encryption')
class WindowsMqttCryptoTests(unittest.TestCase):
    def test_cng_ciphertext_decrypts_to_the_original_fixture_with_valid_random_padding(self):
        import ctypes
        import struct
        from ctypes import wintypes
        from anycubic_cloud._windows_rsa import encrypt_pkcs1
        dll=ctypes.WinDLL('bcrypt.dll')
        handle=ctypes.c_void_p
        size=wintypes.ULONG
        dll.BCryptOpenAlgorithmProvider.argtypes=[ctypes.POINTER(handle),wintypes.LPCWSTR,wintypes.LPCWSTR,size]
        dll.BCryptGenerateKeyPair.argtypes=[handle,ctypes.POINTER(handle),size,size]
        dll.BCryptFinalizeKeyPair.argtypes=[handle,size]
        dll.BCryptExportKey.argtypes=[handle,handle,wintypes.LPCWSTR,ctypes.c_void_p,size,ctypes.POINTER(size),size]
        dll.BCryptDestroyKey.argtypes=[handle]
        dll.BCryptCloseAlgorithmProvider.argtypes=[handle,size]
        algorithm,key=handle(),handle()
        try:
            self.assertEqual(dll.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm),'RSA',None,0),0)
            self.assertEqual(dll.BCryptGenerateKeyPair(algorithm,ctypes.byref(key),2048,0),0)
            self.assertEqual(dll.BCryptFinalizeKeyPair(key,0),0)
            count=size()
            self.assertEqual(dll.BCryptExportKey(key,None,'RSAFULLPRIVATEBLOB',None,0,ctypes.byref(count),0),0)
            output=ctypes.create_string_buffer(count.value)
            self.assertEqual(dll.BCryptExportKey(key,None,'RSAFULLPRIVATEBLOB',output,len(output),ctypes.byref(count),0),0)
            raw=output.raw
            magic,bits,exp_size,mod_size,p_size,q_size=struct.unpack('<6I',raw[:24])
            modulus=raw[24+exp_size:24+exp_size+mod_size]
            exponent=raw[24:24+exp_size]
            # This private key is generated only in memory for an offline test.
            # It is unrelated to the public broker identity and is never saved.
            private_exponent=int.from_bytes(raw[-mod_size:],'big')
            public=struct.pack('<6I',0x31415352,bits,exp_size,mod_size,0,0)+exponent+modulus
            message=b'OFFLINE TEST ONLY'
            ciphertext=encrypt_pkcs1(public,message)
            decoded=pow(int.from_bytes(ciphertext,'big'),private_exponent,int.from_bytes(modulus,'big')).to_bytes(mod_size,'big')
            self.assertEqual(decoded[:2],b'\x00\x02')
            separator=decoded.index(b'\x00',2)
            self.assertGreaterEqual(separator,10)
            self.assertEqual(decoded[separator+1:],message)
            self.assertNotEqual(encrypt_pkcs1(public,message),ciphertext)
        finally:
            if key.value: dll.BCryptDestroyKey(key)
            if algorithm.value: dll.BCryptCloseAlgorithmProvider(algorithm,0)


class CameraLeaseTests(unittest.TestCase):
    def config(self):
        return {'shengwang':{'appid':'a'*32,'channel':'offline-camera','rtc_token':'synthetic-camera-token',
                'client_uid':123,'encryption_mode':'AES_128_GCM2','encryption_key':'offline-key',
                'encryption_kdf_salt':base64.b64encode(bytes(32)).decode()},
                'shengwang_device':{'uid':456},'account_token':'PRIVATE_ACCOUNT'}

    def client(self, data=None):
        client=Mock()
        client.list_printers.return_value=[{'id':12,'device_status':1}]
        client._api.side_effect=[self.config() if data is None else data,{}]
        return client

    def run_camera(self,client,**kwargs):
        from anycubic_cloud.camera import run_camera
        updates=[]
        result=run_camera(client,12,confirmed=True,cancelled=lambda:bool(updates),update=updates.append,**kwargs)
        return result,updates

    def test_camera_requires_explicit_confirmation_before_loading_an_account(self):
        store=Mock()
        with self.assertRaisesRegex(CloudError,'invalid_request'):
            handle_request({'method':'camera','session_path':str((Path.cwd()/'.test/no-account.sec').resolve()),
                            'args':{'printer_id':12}},store_factory=store)
        store.assert_not_called()

    def test_viewer_config_contains_only_short_lived_camera_fields(self):
        from anycubic_cloud.camera import viewer_config
        config=viewer_config(self.config())
        self.assertEqual(config['encryption_mode'],'aes-128-gcm2')
        self.assertEqual(config['device_uid'],456)
        self.assertNotIn('PRIVATE_ACCOUNT',json.dumps(config))
        self.assertEqual(set(config),{'appid','channel','token','uid','device_uid','encryption_mode','encryption_key','salt'})

    def test_camera_open_and_stop_use_only_camera_orders_and_release_before_return(self):
        client=self.client()
        result,updates=self.run_camera(client)
        self.assertEqual(result,{'stopped':True,'cleanup_pending':False})
        self.assertEqual(len(updates),1)
        calls=client._api.call_args_list
        self.assertEqual([c.args[1]['order_id'] for c in calls],['1001',1002])
        self.assertTrue(calls[0].args[1]['shengwang_rtc_support'])
        self.assertTrue(all(c.args[1]['printer_id']==12 for c in calls))

    def test_stop_before_open_does_not_contact_any_printer(self):
        from anycubic_cloud.camera import run_camera
        client=self.client()
        with self.assertRaisesRegex(CloudError,'cancelled'):
            run_camera(client,12,confirmed=True,cancelled=lambda:True,update=lambda data:None)
        client.list_printers.assert_not_called()
        client._api.assert_not_called()

    def test_stop_during_lookup_prevents_a_late_open_order(self):
        from anycubic_cloud.camera import run_camera
        stop=[False]
        client=self.client()
        def lookup():
            stop[0]=True
            return [{'id':12,'device_status':1}]
        client.list_printers.side_effect=lookup
        with self.assertRaisesRegex(CloudError,'cancelled'):
            run_camera(client,12,confirmed=True,cancelled=lambda:stop[0],update=lambda data:None)
        client._api.assert_not_called()

    def test_a_busy_other_viewer_without_credentials_is_never_closed(self):
        client=self.client({})
        with self.assertRaisesRegex(CloudError,'camera_transport_unavailable'):
            self.run_camera(client)
        self.assertEqual(client._api.call_count,1)

    def test_invalid_owned_credentials_are_released_without_being_forwarded(self):
        data=self.config();data['shengwang']['encryption_kdf_salt']='bad'
        client=self.client(data)
        with self.assertRaisesRegex(CloudError,'camera_credentials_invalid'):
            self.run_camera(client)
        self.assertEqual(client._api.call_args_list[-1].args[1]['order_id'],1002)

    def test_unsupported_legacy_transport_releases_its_camera_lease(self):
        client=self.client({'token':{'legacy':'synthetic'}})
        with self.assertRaisesRegex(CloudError,'camera_transport_unavailable'):
            self.run_camera(client)
        self.assertEqual(client._api.call_args_list[-1].args[1]['order_id'],1002)

    def test_failed_cloud_release_remains_visible_in_the_result(self):
        client=self.client()
        client._api.side_effect=[self.config(),CloudError('connection_failed')]
        result,updates=self.run_camera(client)
        self.assertTrue(result['cleanup_pending'])

    def test_unknown_open_outcome_does_not_retry_or_close_an_unowned_session(self):
        client=self.client()
        client._api.side_effect=CloudError('connection_failed')
        with self.assertRaisesRegex(CloudError,'camera_open_unknown'):
            self.run_camera(client)
        self.assertEqual(client._api.call_count,1)

    def test_invalid_encryption_never_falls_back_to_an_unencrypted_viewer(self):
        from anycubic_cloud.camera import viewer_config
        for mode,key in (('unknown','key'),('AES_128_GCM2',''),('none','secret')):
            data=self.config();data['shengwang']['encryption_mode']=mode;data['shengwang']['encryption_key']=key
            with self.assertRaisesRegex(CloudError,'camera_credentials_invalid'):
                viewer_config(data)


class WorkerBoundaryTests(unittest.TestCase):
    def request(self, method, args=None):
        return {"method": method, "args": args or {},
                "session_path": str((Path.cwd() / ".test/worker/account.sec").resolve())}

    def test_account_summary_never_returns_the_account_token(self):
        store = Mock()
        store.load.return_value = Session("synthetic-private-token", "offline@example.invalid")
        response = handle_request(self.request("account"), store_factory=lambda path: store)
        self.assertEqual(response, {"signed_in": True, "email": "offline@example.invalid"})
        self.assertNotIn("synthetic-private-token", json.dumps(response))

    def test_printer_records_are_allowlisted_before_leaving_the_worker(self):
        store, client = Mock(), Mock()
        store.load.return_value = Session("synthetic-private-token")
        client.list_printers.return_value = [{"id": 12, "name": "Offline printer",
                                             "token": "synthetic-private-token", "credentials": {"value": "private"}}]
        response = handle_request(self.request("list_printers"),
                                  client_factory=lambda **kwargs: client, store_factory=lambda path: store)
        self.assertEqual(response, {"printers": [{"id": 12, "name": "Offline printer"}]})

    def test_session_exchange_persists_the_secret_but_returns_only_account_metadata(self):
        store, client = Mock(), Mock()
        session = Session("synthetic-new-private-token", "offline@example.invalid")
        client.exchange_session.return_value = session
        response = handle_request(self.request("exchange_session", {"access_token": "synthetic-import", "client_id": "offline"}),
                                  client_factory=lambda **kwargs: client, store_factory=lambda path: store)
        store.save.assert_called_once_with(session)
        self.assertNotIn("synthetic-new-private-token", json.dumps(response))

    def test_unconfirmed_print_commands_are_rejected_before_loading_any_account(self):
        store_factory = Mock()
        with self.assertRaisesRegex(CloudError, "invalid_request"):
            handle_request(self.request("start_print"), store_factory=store_factory)
        store_factory.assert_not_called()

    def test_relative_session_paths_are_rejected(self):
        request = self.request("account")
        request["session_path"] = "relative/account.sec"
        with self.assertRaisesRegex(CloudError, "invalid_request"):
            handle_request(request)


@unittest.skipUnless(os.name == "nt", "Windows user-scoped secure storage")
class TemperatureControlTests(unittest.TestCase):
    def setUp(self):
        from anycubic_cloud.temperature import set_temperature
        self.send = set_temperature
        root = Path(__file__).resolve().parents[3] / 'cloud-temperature-tests'
        root.mkdir(exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=root, prefix='temperature-')
        self.addCleanup(self.folder.cleanup)
        self.jobs = PrintJobs(Path(self.folder.name) / 'jobs')
        self.client = Mock()
        self.printer = {'id':12, 'model':'Anycubic Kobra X', 'device_status':1, 'is_printing':1}
        self.client.list_printers.return_value = [self.printer]
        self.args = {'printer_id':12, 'heater':'nozzle', 'target':200, 'confirmed':True}

    def test_only_the_selected_heater_is_changed_in_the_cloud_order(self):
        for heater, target, kind, nozzle, bed in [('nozzle',200,0,200,0), ('bed',60,1,0,60),
                                                  ('both',0,2,0,0), ('nozzle',0,0,0,0)]:
            with self.subTest(heater=heater, target=target):
                self.client.reset_mock()
                result = self.send(self.client,self.jobs,{**self.args,'heater':heater,'target':target})
                self.assertEqual(result, {'accepted':True})
                self.client._api.assert_called_once_with(protocol.SEND_ORDER_PATH,
                    {'printer_id':12,'order_id':'1216','data':{'type':kind,
                     'target_nozzle_temp':nozzle,'target_hotbed_temp':bed}},timeout=10)

    def test_invalid_targets_and_unconfirmed_requests_never_read_the_printer_or_send(self):
        for changes in [{'target':True},{'target':200.0},{'target':'200'},{'target':-1},{'target':301},
                        {'heater':'bed','target':101},{'heater':'both','target':1},{'heater':'axis'},
                        {'confirmed':1},{'confirmed':False},{'extra':'G28'},{'printer_id':True}]:
            with self.subTest(changes=changes):
                with self.assertRaises(CloudError):self.send(self.client,self.jobs,{**self.args,**changes})
        self.client.assert_not_called()
        self.client.list_printers.assert_not_called()
        self.client._api.assert_not_called()
        self.assertFalse(self.jobs.root.exists())

    def test_temperature_limits_are_enforced_for_each_heater(self):
        for heater,target in [('nozzle',300),('bed',100)]:
            self.send(self.client,self.jobs,{**self.args,'heater':heater,'target':target})
        self.assertEqual(self.client._api.call_count,2)

    def test_fresh_busy_unknown_offline_or_wrong_printer_blocks_the_command(self):
        for changes in [{'is_printing':2},{'is_printing':None},{'is_printing':True},
                        {'device_status':0},{'model':'Kobra 3'},{'id':13}]:
            with self.subTest(changes=changes):
                self.client.list_printers.return_value = [{**self.printer,**changes}]
                with self.assertRaises(CloudError):self.send(self.client,self.jobs,self.args)
        self.client._api.assert_not_called()

    def test_duplicate_printer_identity_is_rejected(self):
        self.client.list_printers.return_value = [self.printer,self.printer]
        with self.assertRaisesRegex(CloudError,'printer_missing'):self.send(self.client,self.jobs,self.args)
        self.client._api.assert_not_called()

    def test_existing_print_operation_lock_prevents_a_temperature_command(self):
        with self.jobs.lock(12):
            with self.assertRaisesRegex(CloudError,'print_in_progress'):
                self.send(self.client,self.jobs,self.args)
        self.client._api.assert_not_called()

    def test_ambiguous_print_start_protection_is_preserved(self):
        with self.jobs.lock(12):
            self.jobs.save({'schema':1,'printer_id':12,'state':'attempted','ticket':'a'*32})
        with self.assertRaisesRegex(CloudError,'print_locked'):self.send(self.client,self.jobs,self.args)
        self.client._api.assert_not_called()

    def test_cancellation_before_and_after_fresh_status_sends_nothing(self):
        for side_effect in [[True],[False,True]]:
            with self.subTest(side_effect=side_effect):
                with self.assertRaisesRegex(CloudError,'cancelled'):
                    self.send(self.client,self.jobs,self.args,cancelled=Mock(side_effect=side_effect))
        self.client._api.assert_not_called()

    def test_uncertain_network_result_is_reported_without_retry(self):
        self.client._api.side_effect = CloudError('connection_failed')
        with self.assertRaisesRegex(CloudError,'temperature_unconfirmed'):
            self.send(self.client,self.jobs,self.args)
        self.client._api.assert_called_once()

    def test_explicit_server_rejection_is_preserved_without_retry(self):
        self.client._api.side_effect = CloudError('api_rejected',api_code=123)
        with self.assertRaises(CloudError) as raised:self.send(self.client,self.jobs,self.args)
        self.assertEqual(raised.exception.api_code,123)
        self.client._api.assert_called_once()

    def test_worker_rejects_unconfirmed_temperature_request_before_loading_account(self):
        store = Mock()
        with self.assertRaisesRegex(CloudError,'invalid_request'):
            handle_request({'method':'set_temperature','session_path':str(Path(self.folder.name)/'session.sec'),
                            'args':{**self.args,'confirmed':False}},store_factory=store)
        store.assert_not_called()

    def test_confirmed_worker_route_uses_the_bounded_temperature_operation(self):
        store = Mock()
        store.load.return_value = Session('synthetic-temperature-session')
        result = handle_request({'method':'set_temperature',
            'session_path':str(Path(self.folder.name)/'secrets/session.sec'),'args':self.args},
            store_factory=lambda path:store, client_factory=lambda **kwargs:self.client)
        self.assertEqual(result,{'accepted':True})
        self.client._api.assert_called_once()
        self.assertEqual(self.client._api.call_args.args[1]['order_id'],'1216')


class PeripheralControlTests(unittest.TestCase):
    def setUp(self):
        from anycubic_cloud.peripherals import control
        self.control = control
        root = (Path.cwd()/'.test/anycubic-cloud').resolve()
        root.mkdir(parents=True,exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=root,prefix='peripheral-')
        self.addCleanup(self.folder.cleanup)
        self.jobs = PrintJobs(Path(self.folder.name)/'jobs')
        self.client = Mock()
        self.printer = {'id':12,'model':'Kobra X','device_status':1,'is_printing':1}
        self.client.list_printers.return_value = [self.printer]
        self.args = {'printer_id':12,'action':'fan','value':50,'confirmed':True}

    def test_only_part_cooling_fan_percent_is_sent(self):
        for value in [0,50,100]:
            with self.subTest(value=value):
                self.client.reset_mock()
                self.assertEqual(self.control(self.client,self.jobs,{**self.args,'value':value}),{'accepted':True})
                self.client._api.assert_called_once_with(protocol.SEND_ORDER_PATH,
                    {'printer_id':12,'order_id':'1221','data':{'fan_speed_pct':value}},timeout=10)

    def test_light_uses_reported_type_and_does_not_modify_a_running_job(self):
        self.printer['is_printing']=2
        for kind,on in [(0,True),(1,True),(2,False),(40,False)]:
            self.client.reset_mock()
            args = {**self.args,'action':'light','value':on,'light_type':kind}
            self.control(self.client,self.jobs,args)
            self.client._api.assert_called_once_with(protocol.SEND_ORDER_PATH,
                {'printer_id':12,'order_id':'1233','data':{'type':kind,'status':int(on),
                 'brightness':100 if on else 0}},timeout=10)

    def test_light_query_is_bare_and_sends_no_set_data_or_project(self):
        self.control(self.client,self.jobs,{'printer_id':12,'action':'query_light','confirmed':True})
        self.client._api.assert_called_once_with(protocol.SEND_ORDER_PATH,
            {'printer_id':12,'order_id':'1232'},timeout=10)

    def test_invalid_or_unconfirmed_controls_do_no_io(self):
        cases = [{**self.args,'value':v} for v in [True,'50',50.0,-1,101]]
        cases += [{**self.args,'confirmed':1},{**self.args,'extra':'G28'},{**self.args,'action':'hotend_fan'}]
        cases += [{**self.args,'action':'light','value':1,'light_type':2},
                  {**self.args,'action':'light','value':True},
                  {**self.args,'action':'light','value':True,'light_type':-1},
                  {**self.args,'action':'light','value':True,'light_type':2**31},
                  {**self.args,'action':'light','value':True,'light_type':True}]
        for args in cases:
            with self.subTest(args=args):
                with self.assertRaises(CloudError):self.control(self.client,self.jobs,args)
        self.client.list_printers.assert_not_called()
        self.client._api.assert_not_called()
        self.assertFalse(self.jobs.root.exists())

    def test_busy_printer_cannot_change_part_cooling(self):
        self.printer['is_printing']=2
        with self.assertRaisesRegex(CloudError,'printer_busy_or_unknown'):self.control(self.client,self.jobs,self.args)
        self.client._api.assert_not_called()

    def test_light_rechecks_identity_online_and_known_work_state(self):
        for changes in [{'model':'Kobra 3'},{'device_status':0},{'is_printing':True},
                        {'is_printing':None},{'id':13}]:
            self.client.list_printers.return_value = [{**self.printer,**changes}]
            with self.subTest(changes=changes):
                with self.assertRaises(CloudError):
                    self.control(self.client,self.jobs,{**self.args,'action':'light','light_type':1,'value':True})
        self.client._api.assert_not_called()

    def test_control_shares_the_print_start_operation_lock(self):
        with self.jobs.lock(12):
            with self.assertRaisesRegex(CloudError,'print_in_progress'):self.control(self.client,self.jobs,self.args)
        self.client._api.assert_not_called()

    def test_ambiguous_print_start_blocks_part_cooling(self):
        with self.jobs.lock(12):self.jobs.save({'schema':1,'printer_id':12,'state':'attempted','ticket':'a'*32})
        with self.assertRaisesRegex(CloudError,'print_locked'):self.control(self.client,self.jobs,self.args)
        self.client._api.assert_not_called()

    def test_cancelled_commands_are_not_sent(self):
        for values in [[True],[False,True]]:
            with self.assertRaisesRegex(CloudError,'cancelled'):
                self.control(self.client,self.jobs,self.args,cancelled=Mock(side_effect=values))
        self.client._api.assert_not_called()

    def test_uncertain_result_is_never_retried(self):
        self.client._api.side_effect=CloudError('connection_failed')
        with self.assertRaisesRegex(CloudError,'peripheral_unconfirmed'):self.control(self.client,self.jobs,self.args)
        self.client._api.assert_called_once()

    def test_worker_boundary_rejects_unconfirmed_and_routes_confirmed_control(self):
        request = {'method':'peripheral_control','session_path':str(Path(self.folder.name)/'secrets/session.sec'),
                   'args':{**self.args,'confirmed':False}}
        store = Mock()
        with self.assertRaisesRegex(CloudError,'invalid_request'):handle_request(request,store_factory=store)
        store.assert_not_called()
        store.load.return_value=Session('synthetic-peripheral-session')
        request['args']=self.args
        self.assertEqual(handle_request(request,store_factory=lambda path:store,
                         client_factory=lambda **kwargs:self.client),{'accepted':True})
        self.client._api.assert_called_once()


class JobControlTests(unittest.TestCase):
    def setUp(self):
        from anycubic_cloud import job_control
        self.module=job_control
        root=(Path.cwd()/'.test/anycubic-cloud').resolve()
        root.mkdir(parents=True,exist_ok=True)
        self.folder=tempfile.TemporaryDirectory(dir=root,prefix='job-control-')
        self.addCleanup(self.folder.cleanup)
        self.jobs=PrintJobs(Path(self.folder.name)/'print-jobs')
        self.client=Mock()
        self.printer={'id':12,'name':'Offline fixture','model':'Kobra X','device_status':1,'is_printing':2}
        self.project={'id':30,'printer_id':12,'print_status':1,'pause':0,'gcode_name':'offline.gcode'}
        self.client.list_printers.return_value=[self.printer]
        self.client.recent_projects.return_value=[self.project]
        self.now=100

    def review(self,action='pause',**changes):
        return self.module.review(self.client,self.jobs,
            {'printer_id':12,'project_id':30,'action':action,**changes},clock=lambda:self.now)

    def execute(self,plan,**changes):
        return self.module.execute(self.client,self.jobs,
            {'printer_id':12,'ticket':plan['ticket'],'confirmed':True,**changes},clock=lambda:self.now)

    def test_review_does_not_send_a_control_and_binds_the_exact_live_project(self):
        plan=self.review()
        self.assertEqual(plan['project_id'],30)
        self.assertEqual(plan['action'],'pause')
        self.assertEqual(plan['job_name'],'offline.gcode')
        self.client._api.assert_not_called()
        self.assertTrue((self.jobs._folder(12)/'job-control.json').exists())

    def test_each_control_uses_the_exact_project_wire_shape(self):
        for action,paused,order in [('pause',0,2),('resume',1,3),('stop',0,4)]:
            with self.subTest(action=action):
                self.project['pause']=paused
                plan=self.review(action)
                self.client.reset_mock()
                self.assertEqual(self.execute(plan),{'accepted':True})
                self.client._api.assert_called_once_with(protocol.SEND_ORDER_PATH,
                    {'order_id':order,'printer_id':12,'project_id':30,'data':None,'ams_info':None,'settings':None},timeout=10)

    def test_unconfirmed_execute_and_invalid_review_do_no_io(self):
        for args in [{'printer_id':True,'project_id':30,'action':'pause'},
                     {'printer_id':12,'project_id':False,'action':'pause'},
                     {'printer_id':12,'project_id':30,'action':'force_stop'},
                     {'printer_id':12,'project_id':30,'action':'pause','extra':1}]:
            with self.assertRaises(CloudError):self.module.review(self.client,self.jobs,args)
        with self.assertRaisesRegex(CloudError,'invalid_request'):
            self.module.execute(self.client,self.jobs,{'printer_id':12,'ticket':'a'*32,'confirmed':1})
        self.client.list_printers.assert_not_called()
        self.client._api.assert_not_called()
        self.assertFalse(self.jobs.root.exists())

    def test_multiple_old_active_projects_are_never_resolved_by_list_order(self):
        self.client.recent_projects.return_value=[self.project,{**self.project,'id':31}]
        with self.assertRaisesRegex(CloudError,'job_changed'):self.review()
        self.client._api.assert_not_called()

    def test_an_unrelated_historical_job_cannot_be_selected(self):
        self.client.recent_projects.return_value=[{**self.project,'id':29,'print_status':2},self.project]
        with self.assertRaisesRegex(CloudError,'job_changed'):self.review(project_id=29)
        self.client._api.assert_not_called()

    def test_job_changed_during_confirmation_is_rejected(self):
        plan=self.review()
        self.client.recent_projects.return_value=[{**self.project,'id':31}]
        with self.assertRaisesRegex(CloudError,'job_changed'):self.execute(plan)
        self.client._api.assert_not_called()

    def test_pause_changed_during_stop_confirmation_is_rejected(self):
        plan=self.review('stop')
        self.project['pause']=1
        with self.assertRaisesRegex(CloudError,'job_changed'):self.execute(plan)
        self.client._api.assert_not_called()

    def test_offline_idle_other_model_and_unknown_pause_block_review(self):
        for field,value in [('device_status',0),('is_printing',1),('model','Kobra 3')]:
            saved=self.printer[field];self.printer[field]=value
            with self.assertRaises(CloudError):self.review()
            self.printer[field]=saved
        for value in [None,True,2,'unknown']:
            self.project['pause']=value
            with self.assertRaisesRegex(CloudError,'job_state_unknown'):self.review()
        self.client._api.assert_not_called()

    def test_expired_or_clock_reversed_ticket_is_rejected(self):
        for now in [191,99]:
            self.now=100;plan=self.review();self.now=now
            with self.assertRaisesRegex(CloudError,'job_control_expired'):self.execute(plan)
        self.client._api.assert_not_called()

    def test_expiry_during_cloud_recheck_prevents_sending(self):
        plan=self.review()
        def delayed():
            self.now=191
            return [self.project]
        self.client.recent_projects.side_effect=delayed
        with self.assertRaisesRegex(CloudError,'job_control_expired'):self.execute(plan)
        self.client._api.assert_not_called()

    def test_one_shot_ticket_is_consumed_before_the_network_request(self):
        plan=self.review()
        def send(*args,**kwargs):
            record=json.loads((self.jobs._folder(12)/'job-control.json').read_bytes())
            self.assertEqual(record['state'],'attempted')
            raise CloudError('connection_failed')
        self.client._api.side_effect=send
        with self.assertRaisesRegex(CloudError,'job_control_unconfirmed'):self.execute(plan)
        with self.assertRaisesRegex(CloudError,'job_control_consumed'):self.execute(plan)
        self.client._api.assert_called_once()

    def test_uncertain_command_blocks_new_review_until_target_state_is_observed(self):
        plan=self.review()
        self.client._api.side_effect=CloudError('connection_failed')
        with self.assertRaises(CloudError):self.execute(plan)
        with self.assertRaisesRegex(CloudError,'job_control_pending'):self.review('stop')
        self.project['pause']=1
        next_plan=self.review('resume')
        self.assertNotEqual(next_plan['ticket'],plan['ticket'])
        self.client._api.assert_called_once()

    def test_cloud_ack_does_not_synthesize_a_paused_state(self):
        self.execute(self.review())
        self.assertEqual(self.project['pause'],0)
        with self.assertRaisesRegex(CloudError,'job_control_pending'):self.review('stop')
        self.client._api.assert_called_once()

    def test_stop_protection_is_not_bypassed_by_another_confirmation(self):
        self.execute(self.review('stop'))
        with self.assertRaisesRegex(CloudError,'job_control_pending'):self.review('stop')
        self.client._api.assert_called_once()

    def test_rejected_control_can_be_reviewed_again_but_is_never_automatically_retried(self):
        plan=self.review()
        self.client._api.side_effect=CloudError('api_rejected',api_code=321)
        with self.assertRaises(CloudError) as raised:self.execute(plan)
        self.assertEqual(raised.exception.api_code,321)
        self.assertNotEqual(self.review()['ticket'],plan['ticket'])
        self.client._api.assert_called_once()

    def test_a_new_review_invalidates_an_old_unconfirmed_ticket(self):
        old=self.review();self.review('stop')
        with self.assertRaisesRegex(CloudError,'job_control_consumed'):self.execute(old)
        self.client._api.assert_not_called()

    def test_corrupt_state_and_failed_durable_consumption_do_not_send(self):
        plan=self.review()
        with patch('anycubic_cloud.job_control._write',side_effect=OSError('offline disk failure')):
            with self.assertRaises(OSError):self.execute(plan)
        (self.jobs._folder(12)/'job-control.json').write_text('{')
        with self.assertRaisesRegex(CloudError,'job_control_state_unreadable'):self.execute(plan)
        self.client._api.assert_not_called()

    def test_cancellation_and_existing_operation_lock_prevent_commands(self):
        plan=self.review()
        for values in [[True],[False,True]]:
            with self.assertRaisesRegex(CloudError,'cancelled'):
                self.module.execute(self.client,self.jobs,{'printer_id':12,'ticket':plan['ticket'],'confirmed':True},
                    clock=lambda:self.now,cancelled=Mock(side_effect=values))
        with self.jobs.lock(12):
            with self.assertRaisesRegex(CloudError,'print_in_progress'):self.execute(plan)
        self.client._api.assert_not_called()

    def test_worker_guards_confirmation_before_account_and_routes_both_steps(self):
        store=Mock()
        request={'method':'execute_job_control','session_path':str(Path(self.folder.name)/'secrets/session.sec'),
                 'args':{'printer_id':12,'ticket':'a'*32}}
        with self.assertRaisesRegex(CloudError,'invalid_request'):handle_request(request,store_factory=store)
        store.assert_not_called()
        store.load.return_value=Session('synthetic-job-control-session')
        request.update(method='review_job_control',args={'printer_id':12,'project_id':30,'action':'pause'})
        plan=handle_request(request,store_factory=lambda path:store,client_factory=lambda **kwargs:self.client)
        request.update(method='execute_job_control',args={'printer_id':12,'ticket':plan['ticket'],'confirmed':True})
        result=handle_request(request,store_factory=lambda path:store,client_factory=lambda **kwargs:self.client)
        self.assertEqual(result,{'accepted':True})
        self.client._api.assert_called_once()


class CloudLibraryTests(unittest.TestCase):
    def setUp(self):
        from anycubic_cloud.library import list_page
        self.list_page=list_page
        self.client=Mock()
        self.record={'id':30,'old_filename':'Offline model.gcode','filename':'opaque.gcode',
                     'size':'1048576','time':1700000000,'file_extension':'GCODE',
                     'url':'https://invalid.example/PRIVATE?signature=PRIVATE',
                     'thumbnail':'PRIVATE','token':'PRIVATE','path':'PRIVATE','user_id':'PRIVATE'}
        self.client.cloud_files.return_value=[self.record]

    def test_only_safe_metadata_leaves_the_worker(self):
        result=self.list_page(self.client,{'page':1})
        self.assertEqual(result,{'files':[{'id':30,'name':'Offline model.gcode','size':1048576,
            'uploaded_at':'2023-11-14T22:13:20Z','extension':'gcode'}],
            'page':1,'page_size':20,'has_next':False})
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.client.cloud_files.assert_called_once_with(1)
        self.client._api.assert_not_called()

    def test_urls_and_jwt_like_names_do_not_escape_through_filename_fields(self):
        for value in ['https://invalid.example/PRIVATE?signature=PRIVATE',
                      'https%3A%2F%2Finvalid.example%2FPRIVATE%3Fsignature%3DPRIVATE',
                      'file?access_token=PRIVATE','eyJ123456789.abcdefghijk.abcdefghijk',
                      'Bearer PRIVATE','file#PRIVATE']:
            with self.subTest(value=value):
                self.record.update(old_filename=value,filename=value)
                result=self.list_page(self.client,{'page':1})
                self.assertEqual(result['files'][0]['name'],'')
                self.assertNotIn('PRIVATE',json.dumps(result))

    def test_normal_unicode_names_keep_only_the_basename_and_no_control_characters(self):
        self.record['old_filename']='C:\\private\\Žltý model\n.gcode'
        self.assertEqual(self.list_page(self.client,{'page':1})['files'][0]['name'],'Žltý model.gcode')

    def test_missing_name_and_size_are_unknown_instead_of_invented(self):
        self.client.cloud_files.return_value=[{'id':30}]
        file=self.list_page(self.client,{'page':1})['files'][0]
        self.assertEqual(file,{'id':30,'name':'','size':None,'uploaded_at':'','extension':''})

    def test_invalid_sizes_do_not_become_displayed_zero_or_a_large_number(self):
        for value in [True,-1,1.5,'14 MB','PRIVATE',2**63]:
            self.record['size']=value
            self.assertIsNone(self.list_page(self.client,{'page':1})['files'][0]['size'])
        self.record['size']=0
        self.assertEqual(self.list_page(self.client,{'page':1})['files'][0]['size'],0)

    def test_dates_are_validated_and_naive_values_do_not_gain_an_invented_timezone(self):
        cases=[('2026-10-31','2026-10-31'),('2026-10-31 21:16','2026-10-31 21:16:00'),
               ('2026-10-31T21:16:00+01:00','2026-10-31T20:16:00Z'),
               (1700000000000,'2023-11-14T22:13:20Z'),('2026-02-31',''),
               ('PRIVATE',''),('https://invalid.example/PRIVATE',''),(0,''),(True,'')]
        for value,expected in cases:
            self.record['time']=value
            self.assertEqual(self.list_page(self.client,{'page':1})['files'][0]['uploaded_at'],expected)

    def test_extensions_are_bounded_and_can_fall_back_to_the_safe_name(self):
        for value in ['https://PRIVATE','PRIVATE?token=PRIVATE','x'*20,None]:
            self.record['file_extension']=value
            self.assertEqual(self.list_page(self.client,{'page':1})['files'][0]['extension'],'gcode')

    def test_page_bounds_and_unknown_arguments_do_not_fetch(self):
        for args in [{'page':True},{'page':0},{'page':10001},{'page':'1'},
                     {'page':1,'url':'PRIVATE'},{}]:
            with self.assertRaisesRegex(CloudError,'invalid_request'):self.list_page(self.client,args)
        self.client.cloud_files.assert_not_called()

    def test_pagination_uses_full_pages_without_guessing_a_total_count(self):
        self.client.cloud_files.return_value=[{**self.record,'id':i+1} for i in range(20)]
        self.assertTrue(self.list_page(self.client,{'page':2})['has_next'])
        self.assertFalse(self.list_page(self.client,{'page':10000})['has_next'])
        self.client.cloud_files.return_value=[]
        self.assertEqual(self.list_page(self.client,{'page':3})['files'],[])

    def test_malformed_or_duplicate_file_id_and_oversized_pages_are_rejected(self):
        for records in [[None],[{**self.record,'id':True}],[self.record,self.record],
                        [self.record]*21,{'files':[self.record]}]:
            self.client.cloud_files.return_value=records
            with self.assertRaisesRegex(CloudError,'invalid_response'):self.list_page(self.client,{'page':1})

    def test_cancelled_library_fetches_do_not_publish_metadata(self):
        for values in [[True],[False,True]]:
            with self.assertRaisesRegex(CloudError,'cancelled'):
                self.list_page(self.client,{'page':1},cancelled=Mock(side_effect=values))
        self.assertEqual(self.client.cloud_files.call_count,1)

    def test_client_uses_only_the_read_only_listing_endpoint(self):
        calls=[]
        class ListingTransport:
            def request(inner,method,url,body,headers,timeout):
                calls.append((method,url,json.loads(body),timeout))
                return json.dumps({'code':1,'data':[]}).encode()
        client=CloudClient(session=Session('synthetic-library-session'),transport=ListingTransport())
        self.assertEqual(client.cloud_files(2),[])
        self.assertEqual(calls,[('POST',protocol.CLOUD_BASE+protocol.GATEWAY_PREFIX+'/work/index/files',
                                {'page':2,'limit':20},15)])
        for value in [True,0,10001,'2']:
            with self.assertRaisesRegex(CloudError,'invalid_request'):client.cloud_files(value)
        self.assertEqual(len(calls),1)

    def test_client_accepts_null_empty_data_but_rejects_invalid_response_shapes(self):
        client=CloudClient(session=Session('synthetic-library-session'))
        client._api=Mock(return_value=None)
        self.assertEqual(client.cloud_files(1),[])
        for value in [{},[None],[{}]*21]:
            client._api.return_value=value
            with self.assertRaisesRegex(CloudError,'invalid_response'):client.cloud_files(1)

    def test_worker_library_route_requires_no_printer_or_control_confirmation(self):
        store=Mock();store.load.return_value=Session('synthetic-library-session')
        response=handle_request({'method':'cloud_library',
            'session_path':str((Path.cwd()/'.test/library-no-real-account/session.sec').resolve()),
            'args':{'page':1}},store_factory=lambda path:store,client_factory=lambda **kwargs:self.client)
        self.assertEqual(len(response['files']),1)
        self.assertNotIn('PRIVATE',json.dumps(response))
        self.client.list_printers.assert_not_called()
        self.client._api.assert_not_called()


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        root = (Path.cwd() / ".test/anycubic-cloud").resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root, prefix="session-")
        self.assertTrue(Path(self.tmp.name).resolve().is_relative_to(root))
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "account.sec"
        self.store = SessionStore(self.path)

    def test_encrypted_roundtrip_never_stores_the_plaintext_session(self):
        session = Session("synthetic-session-value", "offline@example.invalid")
        self.store.save(session)
        self.assertNotIn(session.token.encode(), self.path.read_bytes())
        self.assertNotIn(session.email.encode(), self.path.read_bytes())
        self.assertEqual(self.store.load(), session)
        self.store.clear()
        self.assertIsNone(self.store.load())

    def test_missing_session_does_not_create_a_file(self):
        self.assertIsNone(self.store.load())
        self.assertFalse(self.path.exists())

    def test_plaintext_session_is_rejected(self):
        self.path.write_text('{"token":"synthetic-session-value","email":""}')
        with self.assertRaisesRegex(CloudError, "secure_session_unreadable"):
            self.store.load()

    def test_boolean_schema_is_rejected(self):
        self.store.save(Session("synthetic-session-value"))
        envelope = json.loads(self.path.read_bytes())
        envelope["schema"] = True
        self.path.write_text(json.dumps(envelope))
        with self.assertRaisesRegex(CloudError, "secure_session_unreadable"):
            self.store.load()

    def test_explicit_encrypted_import_preserves_the_source_file(self):
        source = self.path.parent / "previous-plugin-state.json"
        sealed = _protect(json.dumps({"workbench_token": "synthetic-imported-value", "email": "offline@example.invalid"}).encode())
        source.write_text(json.dumps({"schema": 2, "protection": "windows-user-dpapi",
                                      "sealed": base64.b64encode(sealed).decode()}))
        original = source.read_bytes()
        session = self.store.import_existing(source)
        self.assertEqual(session, Session("synthetic-imported-value", "offline@example.invalid"))
        self.assertEqual(self.store.load(), session)
        self.assertEqual(source.read_bytes(), original)

    def test_explicit_import_rejects_plaintext_without_creating_a_session(self):
        source = self.path.parent / "plaintext.json"
        source.write_text('{"workbench_token":"synthetic-imported-value","email":""}')
        with self.assertRaisesRegex(CloudError, "secure_session_unreadable"):
            self.store.import_existing(source)
        self.assertFalse(self.path.exists())

    def test_corrupted_ciphertext_is_rejected(self):
        self.store.save(Session("synthetic-session-value"))
        envelope = json.loads(self.path.read_bytes())
        envelope["sealed"] = "AAAA"
        self.path.write_text(json.dumps(envelope))
        with self.assertRaisesRegex(CloudError, "secure_session_unreadable"):
            self.store.load()

    def test_protection_failure_writes_no_file(self):
        with patch("anycubic_cloud.session_store._protect", side_effect=RuntimeError("PRIVATE_DETAIL")):
            with self.assertRaisesRegex(CloudError, "secure_session_save_failed"):
                self.store.save(Session("synthetic-session-value"))
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_failed_atomic_replacement_preserves_previous_session_and_removes_temporary_file(self):
        previous = Session("synthetic-previous-value")
        self.store.save(previous)
        with patch("anycubic_cloud.session_store.os.replace", side_effect=OSError("PRIVATE_DETAIL")):
            with self.assertRaisesRegex(CloudError, "secure_session_save_failed"):
                self.store.save(Session("synthetic-next-value"))
        self.assertEqual(self.store.load(), previous)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


class MotionTests(unittest.TestCase):
    def setUp(self):
        from anycubic_cloud.motion import control
        self.control=control
        root=(Path.cwd()/'.test/anycubic-cloud').resolve()
        root.mkdir(parents=True,exist_ok=True)
        folder=tempfile.TemporaryDirectory(dir=root,prefix='motion-')
        self.addCleanup(folder.cleanup)
        self.jobs=PrintJobs(Path(folder.name)/'print-jobs')
        self.client=Mock()
        self.printer={'id':12,'model':'Kobra X','device_status':1,'is_printing':1}
        self.client.list_printers.return_value=[self.printer]
        self.args={'printer_id':12,'axis':'X','direction':'plus','distance':1,'confirmed':True}

    def test_machine_axes_and_signs_are_forwarded_without_bed_inversion(self):
        for axis,number in [('X',1),('Y',2),('Z',3)]:
            for direction,kind in [('minus',0),('plus',1)]:
                for distance in [1,5,10]:
                    self.client.reset_mock()
                    self.control(self.client,self.jobs,{**self.args,'axis':axis,'direction':direction,'distance':distance})
                    self.client._api.assert_called_once_with(protocol.SEND_ORDER_PATH,
                        {'printer_id':12,'order_id':'201','data':{'axis':number,'move_type':kind,'distance':distance}},timeout=10)

    def test_homing_is_explicit_and_has_no_travel_distance(self):
        for axis,number in [('X',1),('Y',2),('Z',3),('ALL',4)]:
            self.client.reset_mock()
            self.control(self.client,self.jobs,{**self.args,'axis':axis,'direction':'home','distance':0})
            self.assertEqual(self.client._api.call_args.args[1]['data'],{'axis':number,'move_type':2,'distance':0})

    def test_extrusion_raw_gcode_and_large_moves_are_rejected_before_io(self):
        cases=[{**self.args,'axis':v} for v in ['E','ALL','G28',None]]
        cases += [{**self.args,'distance':v} for v in [True,0,-1,15,50,1.0,'1']]
        cases += [{**self.args,'direction':'home'},{**self.args,'direction':'stop'},
                  {**self.args,'confirmed':1},{**self.args,'confirmed':False},{**self.args,'extra':'G1 E10'}]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(CloudError): self.control(self.client,self.jobs,args)
        self.client.list_printers.assert_not_called()
        self.client._api.assert_not_called()

    def test_offline_busy_paused_unknown_and_wrong_hardware_cannot_move(self):
        for changes in [{'id':13},{'model':'Kobra S1'},{'device_status':0},{'is_printing':2},
                        {'is_printing':None},{'is_printing':True}]:
            self.client.list_printers.return_value=[{**self.printer,**changes}]
            with self.subTest(changes=changes), self.assertRaises(CloudError): self.control(self.client,self.jobs,self.args)
        self.client._api.assert_not_called()

    def test_motion_shares_the_print_operation_lock(self):
        with self.jobs.lock(12), self.assertRaisesRegex(CloudError,'print_in_progress'):
            self.control(self.client,self.jobs,self.args)
        self.client._api.assert_not_called()

    def test_uncertain_print_start_blocks_motion(self):
        with self.jobs.lock(12): self.jobs.save({'schema':1,'printer_id':12,'state':'attempted','ticket':'a'*32})
        with self.assertRaisesRegex(CloudError,'print_locked'): self.control(self.client,self.jobs,self.args)
        self.client._api.assert_not_called()

    def test_cancelled_motion_is_not_sent(self):
        for sequence in [[True],[False,True]]:
            with self.assertRaisesRegex(CloudError,'cancelled'):
                self.control(self.client,self.jobs,self.args,cancelled=Mock(side_effect=sequence))
        self.client._api.assert_not_called()

    def test_uncertain_motion_is_never_automatically_retried(self):
        self.client._api.side_effect=CloudError('connection_failed')
        with self.assertRaisesRegex(CloudError,'motion_unconfirmed'): self.control(self.client,self.jobs,self.args)
        self.client._api.assert_called_once()

    def test_worker_confirmation_boundary_and_confirmed_route(self):
        request={'method':'motion_control','session_path':str(self.jobs.root.parent/'secrets/session.sec'),
                 'args':{**self.args,'confirmed':False}}
        store=Mock()
        with self.assertRaisesRegex(CloudError,'invalid_request'): handle_request(request,store_factory=store)
        store.assert_not_called()
        store.load.return_value=Session('synthetic-offline-motion')
        request['args']=self.args
        self.assertEqual(handle_request(request,store_factory=lambda path:store,client_factory=lambda **kw:self.client),{'accepted':True})
        self.client._api.assert_called_once()


class JobPreviewTests(unittest.TestCase):
    def setUp(self):
        from anycubic_cloud.job_preview import load
        self.load=load
        self.client=Mock()
        self.client.list_printers.return_value=[{'id':12,'device_status':1,'is_printing':2}]
        self.project={'id':30,'printer_id':12,'print_status':1,
                      'img':'https://workbentch.s3.us-east-2.amazonaws.com/preview.png?signature=PRIVATE'}
        self.client.recent_projects.return_value=[self.project]
        self.image=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jB1cAAAAASUVORK5CYII=')
        self.client._transport.request.return_value=self.image
        self.args={'printer_id':12,'project_id':30}

    def test_only_image_bytes_and_exact_job_identity_reach_native_ui(self):
        result=self.load(self.client,self.args)
        self.assertEqual(base64.b64decode(result['image_base64']),self.image)
        self.assertEqual(result['project_id'],30)
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertNotIn('https',json.dumps(result))
        self.client._transport.request.assert_called_once_with('GET',self.project['img'],None,{'Accept':'image/png,image/jpeg'},8)

    def test_unknown_and_ambiguous_jobs_do_not_download_an_old_preview(self):
        for projects in [[],[{**self.project,'id':29}],[self.project,self.project]]:
            self.client.recent_projects.return_value=projects
            with self.assertRaises(CloudError): self.load(self.client,self.args)
        self.client._transport.request.assert_not_called()

    def test_idle_or_offline_printers_do_not_download_previews(self):
        for record in [{'id':12,'device_status':1,'is_printing':1},{'id':12,'device_status':0,'is_printing':2}]:
            self.client.list_printers.return_value=[record]
            with self.assertRaises(CloudError): self.load(self.client,self.args)
        self.client._transport.request.assert_not_called()

    def test_preview_url_cannot_target_local_arbitrary_or_authenticated_hosts(self):
        for url in ['http://workbentch.s3.us-east-2.amazonaws.com/a','https://127.0.0.1/a',
                    'https://example.com/a','https://workbentch.s3.us-east-2.amazonaws.com.evil.test/a',
                    'https://user:secret@workbentch.s3.us-east-2.amazonaws.com/a',
                    'https://workbentch.s3.us-east-2.amazonaws.com:444/a',
                    'https://workbentch.s3.us-east-2.amazonaws.com/a#secret']:
            self.project['img']=url
            with self.subTest(url=url), self.assertRaises(CloudError): self.load(self.client,self.args)
        self.client._transport.request.assert_not_called()

    def test_relative_image_keys_use_only_the_documented_bucket(self):
        self.project.pop('img')
        self.project['slice_param']={'image_id':'folder/preview.png'}
        self.load(self.client,self.args)
        self.assertEqual(self.client._transport.request.call_args.args[1],
            'https://workbentch.s3.us-east-2.amazonaws.com/folder/preview.png')

    def test_huge_and_non_raster_payloads_are_rejected(self):
        for data in [b'<svg>PRIVATE</svg>',b'x'*(256*1024+1),b'\x89PNG\r\n\x1a\n'+b'\0\0\0\rIHDR'+(5000).to_bytes(4,'big')*2]:
            self.client._transport.request.return_value=data
            with self.assertRaises(CloudError): self.load(self.client,self.args)

    def test_cancellation_before_download_keeps_the_transport_unused(self):
        with self.assertRaisesRegex(CloudError,'cancelled'):
            self.load(self.client,self.args,cancelled=Mock(side_effect=[False,True]))
        self.client._transport.request.assert_not_called()

    def test_jpeg_dimensions_are_bounded_before_native_decoding(self):
        from anycubic_cloud.job_preview import image_size
        self.assertEqual(image_size(b'\xff\xd8\xff\xc0\x00\x0b\x08\x00\x20\x00\x40\x01\x01\x11\x00'),(64,32))


class PortableUpdateTests(unittest.TestCase):
    def setUp(self):
        from anycubic_cloud import portable_update
        self.module=portable_update
        root=(Path.cwd()/'.test/anycubic-cloud').resolve(); root.mkdir(parents=True,exist_ok=True)
        folder=tempfile.TemporaryDirectory(dir=root,prefix='update-'); self.addCleanup(folder.cleanup)
        self.root=Path(folder.name); self.zip=self.root/'update.zip'
        self.files={'orca-slicer.exe':b'synthetic-exe','OrcaSlicer.dll':b'synthetic-dll',
            'resources/scripts/anycubic_cloud_worker.py':b'synthetic-worker',
            'python/python.exe':b'synthetic-python','resources/i18n/en/OrcaSlicer.mo':b'en',
            'resources/i18n/cs/OrcaSlicer.mo':b'cs'}
        self.build={'schema':1,'profile_key':'OrcaCubicCloudDev','channel':'private-anycubic-cloud','revision':'Dev99'}
        self.create()

    def create(self,extras=None,build=None,corrupt=False):
        import zipfile,hashlib
        report={'binary_sha256':{k:hashlib.sha256(v).hexdigest() for k,v in self.files.items() if k.endswith(('.exe','.dll'))},
                'native_module_sha256':{k:hashlib.sha256(v).hexdigest() for k,v in self.files.items() if k.endswith('.py')}}
        if corrupt: report['binary_sha256']['OrcaSlicer.dll']='0'*64
        with zipfile.ZipFile(self.zip,'w') as z:
            for k,v in self.files.items(): z.writestr('package/'+k,v)
            z.writestr('package/OrcaCubicCloudBuild.json',json.dumps(self.build if build is None else build))
            z.writestr('package/Overenie.json',json.dumps(report))
            for k,v in (extras or {}).items(): z.writestr(k,v)

    def test_review_is_read_only_and_does_not_load_an_account(self):
        request={'method':'review_update','session_path':str(self.root/'missing/session.sec'),'args':{'path':str(self.zip)}}
        store=Mock(); client=Mock()
        review=handle_request(request,store_factory=store,client_factory=client)
        self.assertEqual(review['revision'],'Dev99'); self.assertTrue(review['verified'])
        store.assert_not_called(); client.assert_not_called()
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),['update.zip'])

    def test_extraction_always_creates_a_new_folder_and_preserves_old_data(self):
        old=self.root/'old'; old.mkdir(); (old/'session.sec').write_bytes(b'PRIVATE')
        review=self.module.handle({'path':str(self.zip)})
        args={'path':str(self.zip),'output_parent':str(self.root),'sha256':review['sha256'],'confirmed':True}
        result=self.module.handle(args,extract=True)
        target=Path(result['folder']); self.assertEqual(target.parent,self.root)
        self.assertEqual((target/'orca-slicer.exe').read_bytes(),b'synthetic-exe')
        self.assertEqual((old/'session.sec').read_bytes(),b'PRIVATE')
        second=self.module.handle(args,extract=True)
        self.assertNotEqual(second['folder'],result['folder'])

    def test_traversal_absolute_paths_windows_reserved_names_and_secrets_are_rejected(self):
        names=['package/../escape','/absolute','package/C:/escape',
               'package/CON.txt','package/trailing.','package/session.sec','package/OrcaCubicCloudDev.conf',
               'package/nested/state.json','other/root.txt']
        for name in names:
            self.create({name:b'PRIVATE'})
            with self.subTest(name=name),self.assertRaises(CloudError): self.module.handle({'path':str(self.zip)})

    def test_raw_windows_separators_cannot_hide_behind_zip_normalization(self):
        self.create({'package/back/slash':b'bad'})
        self.zip.write_bytes(self.zip.read_bytes().replace(b'package/back/slash',b'package/back'+bytes([92])+b'slash'))
        with self.assertRaises(CloudError): self.module.handle({'path':str(self.zip)})

    def test_case_insensitive_duplicate_names_are_rejected(self):
        self.create({'package/ORCA-SLICER.EXE':b'wrong'})
        with self.assertRaises(CloudError): self.module.handle({'path':str(self.zip)})

    def test_symlinks_are_rejected(self):
        import zipfile,stat
        with zipfile.ZipFile(self.zip,'a') as z:
            info=zipfile.ZipInfo('package/link'); info.external_attr=(stat.S_IFLNK|0o777)<<16
            z.writestr(info,'../../escape')
        with self.assertRaises(CloudError): self.module.handle({'path':str(self.zip)})

    def test_standard_or_other_profile_packages_are_incompatible(self):
        for changes in [{'profile_key':'OrcaSlicer'},{'channel':'public'},{'schema':True},{'revision':'https://PRIVATE'}]:
            self.create(build={**self.build,**changes})
            with self.assertRaises(CloudError): self.module.handle({'path':str(self.zip)})

    def test_modified_critical_files_fail_integrity_checks(self):
        self.create(corrupt=True)
        with self.assertRaises(CloudError): self.module.handle({'path':str(self.zip)})

    def test_package_change_after_review_prevents_extraction(self):
        review=self.module.handle({'path':str(self.zip)})
        self.create({'package/extra.txt':b'changed'})
        with self.assertRaisesRegex(CloudError,'update_changed'):
            self.module.handle({'path':str(self.zip),'output_parent':str(self.root),'sha256':review['sha256'],'confirmed':True},extract=True)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),['update.zip'])

    def test_extraction_requires_explicit_confirmation_and_cancellation_does_not_write(self):
        review=self.module.handle({'path':str(self.zip)})
        args={'path':str(self.zip),'output_parent':str(self.root),'sha256':review['sha256'],'confirmed':False}
        with self.assertRaises(CloudError): self.module.handle(args,extract=True)
        with self.assertRaisesRegex(CloudError,'cancelled'):
            self.module.handle({'path':str(self.zip)},cancelled=lambda:True)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),['update.zip'])


if __name__ == "__main__":
    unittest.main()
