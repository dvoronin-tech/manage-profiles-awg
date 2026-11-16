import argparse
import getpass
import json
import logging
import os
from pathlib import Path
import sys
import warnings

import paramiko

from profile_name import PROFILE_NAME_RULE, valid_profile_name


KNOWN_HOSTS = "/ssh/known_hosts"
IDENTITY_FILE = Path("/ssh/identity")
PROFILE_DIRECTORY = Path("/profiles")
REMOTE_COMMAND = "docker exec -i awg-profile-manager python3 /app/server.py 2>/dev/null"

ssh_logger = logging.getLogger("paramiko")
ssh_logger.addHandler(logging.NullHandler())
ssh_logger.propagate = False


class ClientError(Exception):
    pass


def ssh_port(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("SSH port must be a number.") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("SSH port must be between 1 and 65535.")
    return port


def build_parser():
    parser = argparse.ArgumentParser(description="Manage AmneziaWG profiles over SSH.")
    password_options = parser.add_mutually_exclusive_group()
    password_options.add_argument("--password", action="store_true", help="Prompt for a password if keys fail")
    password_options.add_argument("--password-stdin", action="store_true", help="Read a password from redirected stdin if keys fail")
    parser.add_argument("--host", required=True, help="VPS hostname or IP address")
    parser.add_argument("--user", required=True, help="VPS SSH username")
    parser.add_argument("--port", type=ssh_port, default=22, help="VPS SSH port")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="List existing VPN profiles")
    create = commands.add_parser("create", help="Create one VPN profile")
    create.add_argument("name", help="Profile name")
    revoke = commands.add_parser("revoke", help="Revoke one VPN profile after confirmation")
    selector = revoke.add_mutually_exclusive_group(required=True)
    selector.add_argument("--name", help="Exact name of the profile to revoke")
    selector.add_argument("--address", help="VPN address of an unnamed profile")
    return parser


def supplied_password(args, key_error):
    try:
        if args.password:
            if not sys.stdin.isatty():
                raise ClientError("SSH password entry needs a terminal. Use --password-stdin with redirected input.")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("VPS SSH password: ")
        elif args.password_stdin:
            if sys.stdin.isatty():
                raise ClientError("Use --password for hidden entry at a terminal.")
            password = sys.stdin.readline().removesuffix("\n")
        else:
            raise ClientError(f"{key_error} No SSH password was supplied. Use --password or --password-stdin.")
    except (EOFError, OSError, UnicodeError, getpass.GetPassWarning):
        raise ClientError("Could not read the SSH password securely.") from None
    except KeyboardInterrupt:
        raise ClientError("SSH password entry cancelled.") from None
    if not password:
        raise ClientError(f"{key_error} No SSH password was supplied.")
    return password


def authentication_interrupted(transport, error):
    # Paramiko uses AuthenticationException for timeouts as well as rejections.
    return not transport.is_active() or str(error) == "Authentication timeout."


def authenticate_password(transport, username, password):
    answered = False

    def answer_password(_title, _instructions, prompts):
        nonlocal answered
        if not prompts:
            return []
        if answered or len(prompts) != 1 or prompts[0][1]:
            raise ClientError("The VPS requires more than one hidden password prompt or another authentication method.")
        answered = True
        return [password]

    try:
        try:
            transport.auth_password(username, password, fallback=False)
        except paramiko.BadAuthenticationType as error:
            if authentication_interrupted(transport, error) or "keyboard-interactive" not in error.allowed_types:
                raise
            transport.auth_interactive(username, answer_password)
    except paramiko.AuthenticationException as error:
        if authentication_interrupted(transport, error):
            raise
        if isinstance(error, paramiko.BadAuthenticationType):
            raise ClientError("The VPS does not allow SSH password authentication.") from None
        raise ClientError("The VPS rejected the SSH password.") from None
    if not transport.is_authenticated():
        raise ClientError("The SSH password did not complete authentication. The VPS requires another method.")


class KeyThenPassword(paramiko.AuthStrategy):
    def __init__(self, args):
        super().__init__(ssh_config=None)
        self.args = args

    def authenticate(self, transport):
        # SSHClient calls this only after verifying the remote host key.
        username = self.args.user
        key_error = "No SSH key is available. Load a key or set AWG_SSH_KEY."
        keys = ()
        agent = None
        if IDENTITY_FILE.is_file():
            try:
                keys = (paramiko.PKey.from_path(IDENTITY_FILE),)
            except (TypeError, paramiko.PasswordRequiredException):
                key_error = "Could not unlock the SSH identity file. Load the key into your Mac's SSH agent."
            except (OSError, ValueError, paramiko.SSHException, paramiko.UnknownKeyType):
                key_error = "Could not load the SSH identity file."
        elif os.environ.get("SSH_AUTH_SOCK"):
            try:
                agent = paramiko.Agent()
                keys = agent.get_keys()
            except (OSError, paramiko.SSHException):
                key_error = "Could not read the Mac's SSH agent."

        try:
            for key in keys:
                try:
                    transport.auth_publickey(username, key)
                except paramiko.AuthenticationException as error:
                    if authentication_interrupted(transport, error):
                        raise
                    key_error = "The VPS rejected SSH key authentication."
                    if isinstance(error, paramiko.BadAuthenticationType):
                        break
                else:
                    if transport.is_authenticated():
                        return
                    key_error = "The SSH key requires another authentication method."
                    break
        finally:
            if agent is not None:
                agent.close()

        if not transport.is_active():
            raise ClientError("The verified SSH connection closed before password authentication.")
        authenticate_password(transport, username, supplied_password(self.args, key_error))


def connect(args):
    ssh = paramiko.SSHClient()
    try:
        ssh.load_host_keys(KNOWN_HOSTS)
    except (OSError, ValueError, paramiko.SSHException):
        raise ClientError("Could not load the SSH known_hosts file.") from None

    host_key_name = args.host if args.port == 22 else f"[{args.host}]:{args.port}"
    if host_key_name not in ssh.get_host_keys():
        raise ClientError("The VPS has no trusted key in SSH known_hosts. Verify and save its host key first.")

    try:
        ssh.connect(
            hostname=args.host,
            port=args.port,
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
            auth_strategy=KeyThenPassword(args),
        )
    except paramiko.BadHostKeyException:
        ssh.close()
        raise ClientError("The VPS SSH host key does not match known_hosts.") from None
    except ClientError:
        ssh.close()
        raise
    except (OSError, EOFError, paramiko.SSHException):
        ssh.close()
        raise ClientError("Could not establish a verified SSH connection to the VPS.") from None
    return ssh


def request(ssh, payload):
    try:
        _, stdout, _ = ssh.exec_command(REMOTE_COMMAND, timeout=60)
        with stdout.channel as channel:
            channel.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            channel.shutdown_write()
            output = stdout.read()
            status = channel.recv_exit_status()
    except (OSError, EOFError, KeyboardInterrupt, paramiko.SSHException):
        raise ClientError("The SSH request did not finish. The operation may have reached the VPS.") from None

    if status:
        raise ClientError("The profile manager did not finish. The operation may have reached the VPS. Check that its container is running and the SSH account can run Docker.")
    try:
        reply = json.loads(output)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ClientError("The VPS returned an invalid response. The operation may have reached the VPS.") from None
    if isinstance(reply, dict):
        if reply.get("ok") is True:
            return reply
        if reply.get("ok") is False and isinstance(reply.get("error"), str):
            raise ClientError(reply["error"])
    raise ClientError("The VPS returned an invalid response. The operation may have reached the VPS.")


def terminal_text(value):
    return "".join(character if character.isprintable() else ascii(character)[1:-1] for character in value)


def list_profiles(ssh):
    reply = request(ssh, {"action": "list"})
    profiles = reply["profiles"]
    if not profiles:
        print("No VPN profiles.")
        return

    print("NAME\tVPN ADDRESS")
    for profile in profiles:
        name = profile["name"]
        label = terminal_text(name) if name else "(unnamed)"
        addresses = ", ".join(profile["addresses"]) or "(none)"
        print(f"{label}\t{addresses}")


def is_reserved_file(path, file):
    return os.path.samestat(path.lstat(), os.fstat(file.fileno()))


def create_profile(args):
    if not valid_profile_name(args.name):
        raise ClientError(PROFILE_NAME_RULE)
    destination = PROFILE_DIRECTORY / f"{args.name}.conf"
    display_path = Path(os.environ.get("AWG_OUTPUT_DIRECTORY", str(PROFILE_DIRECTORY))) / destination.name
    try:
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ClientError(f"The local profile already exists at {display_path}.") from None
    except OSError:
        raise ClientError(f"Could not reserve the local profile path {display_path}.") from None

    with os.fdopen(descriptor, "wb", buffering=0) as file:
        saved = False
        try:
            with connect(args) as ssh:
                reply = request(ssh, {"action": "create", "name": args.name, "host": args.host})
            config = reply.get("config")
            if not isinstance(config, str) or not config.startswith("[Interface]\n"):
                raise ClientError("The VPS reported success without a usable configuration. The profile may already exist.")
            try:
                if not is_reserved_file(destination, file):
                    raise OSError("Profile path was replaced")
                remaining = memoryview(config.encode("utf-8"))
                while remaining:
                    written = file.write(remaining)
                    if not written:
                        raise OSError("Could not write the VPN profile")
                    remaining = remaining[written:]
                os.fsync(file.fileno())
                if not is_reserved_file(destination, file):
                    raise OSError("Profile path was replaced")
            except (OSError, UnicodeError):
                raise ClientError(f"The profile was created on the VPS, but the configuration could not be saved at {display_path}.") from None
            saved = True
        finally:
            if not saved:
                try:
                    if is_reserved_file(destination, file):
                        destination.unlink()
                except OSError:
                    pass
    print(f"Saved {terminal_text(str(display_path))}")


def revoke_profile(ssh, args):
    selector = {"name": args.name} if args.name is not None else {"address": args.address}
    try:
        preview = request(ssh, {"action": "preview-revoke", **selector})
        label, selection = preview.get("label"), preview.get("selection")
        if not isinstance(label, str) or not isinstance(selection, str):
            raise ClientError("The VPS returned an invalid profile selection.")
    except ClientError as error:
        raise ClientError(f"{error} No revocation was sent.") from None

    label = terminal_text(label)
    host = terminal_text(args.host)
    target = f"[{host}]:{args.port}" if ":" in host else f"{host}:{args.port}"
    print(f'Revoke VPN profile "{label}" on VPS {target}? [y/N] ', end="", file=sys.stderr, flush=True)
    try:
        confirmed = input().strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt, OSError):
        confirmed = False
    if not confirmed:
        raise ClientError("Revocation cancelled. No revocation was sent.")

    request(ssh, {"action": "revoke", "selection": selection, **selector})
    print(f'Revoked VPN profile "{label}" on VPS {target}.')


def main():
    args = build_parser().parse_args()
    try:
        if args.action == "create":
            create_profile(args)
        else:
            if args.action == "revoke" and not sys.stdin.isatty():
                raise ClientError("Revocation requires an interactive terminal for confirmation. No revocation was sent.")
            with connect(args) as ssh:
                if args.action == "revoke":
                    revoke_profile(ssh, args)
                else:
                    list_profiles(ssh)
    except ClientError as error:
        print(terminal_text(str(error)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
