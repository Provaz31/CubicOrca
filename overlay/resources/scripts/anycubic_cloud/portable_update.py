"""Local-only portable package review/extraction. No network, execution or profile writes."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import re
import stat
import uuid
import zipfile
from .client import CloudError

FORBIDDEN = {'session.sec','state.json','job-control.json','current.json','operation.lock',
             'orcacubicclouddev.conf','orca_refresh_token.sec','.install_state.json'}


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()


def _json(package, name):
    info = package.getinfo(name)
    if info.file_size > 256*1024: raise CloudError('update_invalid')
    value = json.loads(package.read(info))
    if not isinstance(value, dict): raise CloudError('update_invalid')
    return value


def inspect(path, *, cancelled=lambda:False):
    source = Path(path)
    if not source.is_absolute() or source.suffix.lower() != '.zip' or not source.is_file():
        raise CloudError('update_invalid')
    if source.stat().st_size > 1024**3: raise CloudError('update_invalid')
    checksum = digest(source)
    if cancelled(): raise CloudError('cancelled')
    try:
        with zipfile.ZipFile(source) as package:
            entries=package.infolist()
            if not 1 <= len(entries) <= 30000 or sum(i.file_size for i in entries) > 3*1024**3:
                raise CloudError('update_invalid')
            seen=set(); roots=set()
            for info in entries:
                name=info.orig_filename
                parts=name.rstrip('/').split('/')
                if (not parts or name!=info.filename or name.startswith('/') or chr(92) in name or ':' in name or
                        any(p in ('','.','..') or p.endswith((' ','.')) or len(p)>200 or
                            any(ord(c)<32 for c in p) or
                            re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',p,re.I) for p in parts) or
                        any(p.lower() in FORBIDDEN for p in parts) or
                        stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1 or
                        name.rstrip('/').casefold() in seen):
                    raise CloudError('update_invalid')
                seen.add(name.rstrip('/').casefold()); roots.add(parts[0])
            if len(roots)!=1: raise CloudError('update_invalid')
            prefix=next(iter(roots))+'/'
            build=_json(package,prefix+'OrcaCubicCloudBuild.json')
            if (type(build.get('schema')) is not int or build.get('schema')!=1 or build.get('profile_key')!='OrcaCubicCloudDev' or
                    build.get('channel')!='private-anycubic-cloud' or
                    not isinstance(build.get('revision'),str) or not re.fullmatch(r'Dev[0-9]+[a-z]?',build['revision'])):
                raise CloudError('update_incompatible')
            for required in ('python/python.exe','resources/i18n/en/OrcaSlicer.mo','resources/i18n/cs/OrcaSlicer.mo'):
                if package.getinfo(prefix+required).file_size<=0: raise CloudError('update_invalid')
            report=_json(package,prefix+'Overenie.json')
            hashes={**report.get('binary_sha256',{}),**report.get('native_module_sha256',{})}
            if not {'orca-slicer.exe','OrcaSlicer.dll','resources/scripts/anycubic_cloud_worker.py'} <= hashes.keys():
                raise CloudError('update_invalid')
            for relative,expected in hashes.items():
                if not isinstance(relative,str) or not isinstance(expected,str) or not re.fullmatch('[a-f0-9]{64}',expected):
                    raise CloudError('update_invalid')
                hashed=hashlib.sha256()
                with package.open(prefix+relative) as stream:
                    for chunk in iter(lambda:stream.read(1024*1024),b''): hashed.update(chunk)
                if hashed.hexdigest()!=expected:
                    raise CloudError('update_invalid')
                if cancelled(): raise CloudError('cancelled')
            if package.testzip() is not None: raise CloudError('update_invalid')
            # A checksum detects changes; an unsigned manifest does not authenticate its author.
            return {'revision':build['revision'],'sha256':checksum,'files':len(entries),'verified':True}
    except CloudError: raise
    except Exception: raise CloudError('update_invalid') from None


def handle(arguments, *, extract=False, cancelled=lambda:False):
    keys={'path','output_parent','sha256','confirmed'} if extract else {'path'}
    if not isinstance(arguments,dict) or set(arguments)!=keys or (extract and arguments['confirmed'] is not True):
        raise CloudError('invalid_request')
    path=arguments.get('path')
    if not isinstance(path,str): raise CloudError('invalid_request')
    review=inspect(path,cancelled=cancelled)
    if not extract: return review
    if arguments['sha256']!=review['sha256']: raise CloudError('update_changed')
    parent=Path(arguments['output_parent']) if isinstance(arguments['output_parent'],str) else Path()
    if not parent.is_absolute() or not parent.is_dir(): raise CloudError('update_invalid')
    target=(parent.resolve()/('OrcaCubic_Cloud_'+review['revision']+'_'+uuid.uuid4().hex[:8])).resolve()
    if target.parent!=parent.resolve() or target.exists(): raise CloudError('update_invalid')
    if cancelled(): raise CloudError('cancelled')
    target.mkdir() # Always new; never replace an existing application or the profile.
    try:
        with zipfile.ZipFile(path) as package:
            for info in package.infolist():
                if cancelled(): raise CloudError('cancelled')
                relative=PurePosixPath(info.filename).parts[1:]
                dest=target.joinpath(*relative).resolve()
                if not dest.is_relative_to(target): raise CloudError('update_invalid')
                if info.is_dir(): dest.mkdir(parents=True,exist_ok=True); continue
                dest.parent.mkdir(parents=True,exist_ok=True)
                with package.open(info) as src,dest.open('xb') as out:
                    for data in iter(lambda:src.read(1024*1024),b''): out.write(data)
        if digest(Path(path))!=review['sha256']: raise CloudError('update_changed')
        return {'revision':review['revision'],'folder':str(target),'extracted':True}
    except CloudError: raise
    except Exception: raise CloudError('update_extract_failed') from None
