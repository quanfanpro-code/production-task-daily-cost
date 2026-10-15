"""统一备份与写入；不删除原件或历史文件。"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def backup(path):
    path=Path(path).resolve()
    if not path.exists():
        return None
    base=Path(os.environ.get('USERPROFILE',str(Path.home())))/'BackUp'
    target=base/('生产任务逐日_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))/path.drive.replace(':','')/Path(*path.parts[1:])
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(path,target)
    if digest(target)!=digest(path):
        raise OSError('备份校验失败：'+str(path))
    return target


def write_bytes(path,data):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and path.read_bytes()==data:
        return path
    backup(path)
    part=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
    with part.open('xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part,path)
    if path.read_bytes()!=data:
        raise OSError('写入回读校验失败：'+str(path))
    return path


def write_json(path,value):
    data=json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)
    json.loads(data)
    return write_bytes(path,data.encode('utf-8'))


def write_text(path,text):
    return write_bytes(path,text.encode('utf-8-sig'))


def safe_child(root,relative):
    root=Path(root).resolve()
    value=(root/relative).resolve()
    if not value.is_relative_to(root) or value==root:
        raise ValueError('路径超出指定目录：'+str(relative))
    return value


def separate_output(source,output):
    source=Path(source).resolve();output=Path(output).resolve()
    if source==output or source.is_relative_to(output):
        raise ValueError('输出目录不能等于或包含原件目录')
    return source,output
