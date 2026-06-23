import pathlib

_UUID_FILE = pathlib.Path.home() / '.rmbt_nettest_uuid'


def load():
    try:
        v = _UUID_FILE.read_text().strip()
        return v if v else None
    except OSError:
        return None


def save(uuid):
    _UUID_FILE.write_text(uuid + '\n')
    print(f'Client UUID saved: {uuid}\n  ({_UUID_FILE})')
