"""Install/check the Mac timer links. Never reads Oracle credentials."""
import ast
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape

LABEL='dev.gendi.oracle-vps-autoprovision'
REPO=Path(__file__).resolve().parent.parent


def interval(repo):
    tree=ast.parse((repo/'local_timer.py').read_text())
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='INTERVAL' for t in node.targets):
            value=ast.literal_eval(node.value)
            if isinstance(value,int) and value>0:
                return value
    raise ValueError('No valid INTERVAL found in local_timer.py')


def layout(repo, home):
    runtime=home/'Library/Application Support/OracleAutoProvision'
    agent=home/'Library/LaunchAgents'/f'{LABEL}.plist'
    links={runtime/'local_timer.py':repo/'local_timer.py',
           runtime/'diagnostics.py':repo/'diagnostics.py'}
    return runtime,agent,links


def render(repo,runtime,python,gh):
    values={'PYTHON':python,'RUNTIME':str(runtime),'INTERVAL':str(interval(repo)),
            'PATH':':'.join(dict.fromkeys([str(Path(gh).parent),str(Path(python).parent),'/usr/bin','/bin']))}
    text=(repo/'macos/launchagent.plist.template').read_text()
    for key,value in values.items():
        text=text.replace('@@'+key+'@@',escape(value))
    data=plistlib.loads(text.encode())
    if data['Label']!=LABEL:
        raise ValueError('Unexpected LaunchAgent label in template')
    return plistlib.dumps(data)


def validate_existing(repo,home):
    runtime,agent,links=layout(repo,home)
    if agent.is_symlink():
        legacy=repo/'.local'/f'{LABEL}.plist'
        if agent.resolve()!=legacy.resolve():
            raise ValueError(f'Refusing to replace an unrelated LaunchAgent symlink: {agent}')
    elif agent.exists():
        if not agent.is_file() or plistlib.loads(agent.read_bytes()).get('Label')!=LABEL:
            raise ValueError('Refusing to replace another LaunchAgent')
    for destination,target in links.items():
        if destination.is_symlink():
            if destination.resolve()!=target.resolve():
                raise ValueError(f'Refusing to replace an unrelated symlink: {destination}')
        elif destination.exists():
            if not destination.is_file():
                raise ValueError(f'Expected a file, not a directory: {destination}')
            if destination.read_bytes()!=target.read_bytes():
                raise ValueError(f'Installed copy differs from repository; review before migration: {destination}')


def replace_link(destination,target):
    if destination.is_symlink() and destination.resolve()==target.resolve():
        return
    temporary=destination.with_name(destination.name+'.installing')
    if temporary.exists() or temporary.is_symlink():
        raise ValueError(f'Unexpected unfinished installation file: {temporary}')
    temporary.symlink_to(target)
    os.replace(temporary,destination)


def write_layout(repo,home,python,gh):
    """Filesystem-only part, also exercised with a temporary home in tests."""
    runtime,agent,links=layout(repo,home)
    runtime.mkdir(parents=True,mode=0o700,exist_ok=True)
    runtime.chmod(0o700)
    agent.parent.mkdir(parents=True,exist_ok=True)
    temporary=agent.with_suffix('.plist.installing')
    if temporary.exists() or temporary.is_symlink():
        raise ValueError(f'Unexpected unfinished installation file: {temporary}')
    temporary.write_bytes(render(repo,runtime,python,gh))
    temporary.chmod(0o600)
    # Atomic replacement also replaces the legacy symlink, without following it.
    os.replace(temporary,agent)
    log=runtime/'timer.log'
    log.touch(exist_ok=True)
    log.chmod(0o600)
    for destination,target in links.items():
        replace_link(destination,target)
    legacy=repo/'.local'/f'{LABEL}.plist'
    if legacy.is_file() and not legacy.is_symlink() and plistlib.loads(legacy.read_bytes()).get('Label')==LABEL:
        legacy.unlink()
        if not any(legacy.parent.iterdir()):
            legacy.parent.rmdir()


def check_layout(repo,home):
    runtime,agent,links=layout(repo,home)
    errors=[]
    for destination,target in links.items():
        if not destination.is_symlink() or destination.resolve()!=target.resolve() or not target.is_file():
            errors.append(f'Missing/wrong symlink: {destination} -> {target}')
    if not agent.is_file() or agent.is_symlink():
        errors.append('LaunchAgent plist must be a regular file in ~/Library/LaunchAgents')
    else:
        try:
            config=plistlib.loads(agent.read_bytes())
            expected={'Label':LABEL,'StartInterval':interval(repo),'WorkingDirectory':str(runtime),
                      'StandardOutPath':str(runtime/'timer.log'),'StandardErrorPath':str(runtime/'timer.log')}
            for key,value in expected.items():
                if config.get(key)!=value:
                    errors.append(f'Unexpected plist field: {key}')
            args=config.get('ProgramArguments',[])
            if len(args)!=2 or args[1]!=str(runtime/'local_timer.py') or not os.access(args[0],os.X_OK):
                errors.append('Invalid Python executable or timer path')
            if config.get('EnvironmentVariables',{}).get('PYTHONDONTWRITEBYTECODE')!='1':
                errors.append('Bytecode-cache setting is missing')
            if config.get('RunAtLoad') is not True:
                errors.append('RunAtLoad is disabled')
            if agent.stat().st_mode & 0o077:
                errors.append('LaunchAgent plist must have permissions 600')
        except (ValueError,plistlib.InvalidFileException):
            errors.append('Generated plist cannot be parsed')
    if not runtime.is_dir() or runtime.stat().st_mode & 0o077:
        errors.append('Runtime directory must have permissions 700')
    return errors


def service():
    return subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/{LABEL}'],capture_output=True,text=True)


def install():
    python=shutil.which('python3')
    gh=shutil.which('gh')
    if not python or not gh:
        raise ValueError('Install python3 and GitHub CLI (gh) first')
    home=Path.home()
    validate_existing(REPO,home)
    if service().returncode==0:
        subprocess.run(['/bin/launchctl','bootout',f'gui/{os.getuid()}/{LABEL}'],check=True)
    write_layout(REPO,home,python,gh)
    runtime,agent,links=layout(REPO,home)
    subprocess.run(['/usr/bin/plutil','-lint',str(agent)],check=True)
    subprocess.run(['/bin/launchctl','bootstrap',f'gui/{os.getuid()}',str(agent)],check=True)
    print(f'Installed two code symlinks and LaunchAgent plist; interval={interval(REPO)} seconds')
    return check()


def check():
    errors=check_layout(REPO,Path.home())
    if not shutil.which('gh'):
        errors.append('GitHub CLI (gh) is missing')
    loaded=service()
    if loaded.returncode!=0:
        errors.append('LaunchAgent is not loaded in the current user session')
    elif f'run interval = {interval(REPO)} seconds' not in loaded.stdout:
        errors.append('Loaded LaunchAgent interval differs from repository; rerun install.sh')
    if errors:
        for error in errors:
            print('FAIL: '+error)
        return 1
    print('OK: two code symlinks point to this repository; LaunchAgents plist valid; service loaded')
    last=re.search(r'last exit code = (-?\d+)',loaded.stdout)
    if last:
        print('Last timer exit code: '+last.group(1)+' (details in timer.log)')
    print('Log: '+str(layout(REPO,Path.home())[0]/'timer.log'))
    return 0


def main():
    if sys.platform!='darwin':
        print('This installer/checker requires macOS',file=sys.stderr)
        return 1
    os.umask(0o077)
    try:
        return install() if sys.argv[1:] == ['install'] else check() if sys.argv[1:] == ['check'] else 2
    except (OSError,ValueError,subprocess.CalledProcessError) as error:
        print('FAIL: '+str(error),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
