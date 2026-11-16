import base64
from dataclasses import dataclass
import fcntl
import hashlib
from ipaddress import IPv4Address, ip_address, ip_interface, ip_network
import json
import os
import re
import subprocess
import sys

from profile_name import PROFILE_NAME_RULE, valid_profile_name


AWG_CONTAINER = os.environ.get("AWG_CONTAINER", "amnezia-awg")
AWG_INTERFACE = os.environ.get("AWG_INTERFACE", "awg0")
AWG_CLIENTS_TABLE = os.environ.get("AWG_CLIENTS_TABLE", "/opt/amnezia/awg/clientsTable")
MAX_REQUEST_BYTES = 16_384
LOCK_PATH = "/tmp/awg-profile-manager.lock"
SECTION_HEADER = re.compile(r"(?m)^[ \t]*\[([^\]\r\n]+)\][ \t]*(?:#[^\r\n]*)?\r?$")
AWG_INTERFACE_KEYS = (
    "Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4",
    "I1", "I2", "I3", "I4", "I5", "HeaderProtectionKey", "ContentPaddingAddition",
    "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime", "KeepaliveTimeout",
    "MaxHandshakeAttempts", "RandomTrailers", "DisableCookies",
)


class ServerError(Exception):
    pass


class MutationUncertain(ServerError):
    pass


@dataclass(frozen=True)
class Profile:
    public_key: str
    name: str | None
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class ConfigSection:
    kind: str
    text: str
    fields: dict[str, list[str]]

    def field(self, key):
        return self.fields.get(key.lower(), [""])[-1]


def docker_command(*args, error, input_text=None, timeout=15):
    try:
        return subprocess.run(
            ["docker", *args],
            input=input_text.encode("utf-8") if input_text is not None else None,
            capture_output=True,
            check=True,
            timeout=timeout,
        ).stdout.decode("utf-8")
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise ServerError(error) from None


def docker_exec(*args, error, input_text=None, timeout=15):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", AWG_CONTAINER):
        raise ServerError("AWG_CONTAINER is not a valid Docker container name.")
    command = ["exec"]
    if input_text is not None:
        command.append("-i")
    return docker_command(
        *command, AWG_CONTAINER, *args, error=error, input_text=input_text, timeout=timeout,
    )


def docker_mutation(*args, error, input_text=None):
    # Stopping the Docker CLI would not prove that its container command stopped.
    try:
        return docker_exec(*args, error=error, input_text=input_text, timeout=None).strip()
    except ServerError:
        raise MutationUncertain(error) from None


def mutate_peer(*args, error, input_text=None):
    result = docker_mutation(
        "sh", "-c", 'if "$@" >/dev/null; then echo updated; else echo failed; fi',
        "sh", "awg", *args, error=error, input_text=input_text,
    )
    if result == "failed":
        raise ServerError(error)
    if result != "updated":
        raise MutationUncertain(error)


def config_path():
    if override := os.environ.get("AWG_CONFIG_PATH"):
        if not override.startswith("/"):
            raise ServerError("AWG_CONFIG_PATH must be an absolute path inside the AmneziaWG container.")
        candidates = (override,)
    else:
        candidates = (
            f"/etc/amneziawg/{AWG_INTERFACE}.conf",
            f"/opt/amnezia/awg/{AWG_INTERFACE}.conf",
        )
    return docker_exec(
        "sh", "-c", 'for path do if [ -f "$path" ]; then printf "%s" "$path"; exit 0; fi; done; exit 1',
        "sh", *candidates,
        error="Could not locate the AmneziaWG configuration. Set AWG_CONFIG_PATH for this installation.",
    )


def read_config(path):
    return docker_exec("cat", path, error="Could not read the persistent AmneziaWG configuration.")


def config_sections(config):
    headers = list(SECTION_HEADER.finditer(config))
    sections = [ConfigSection("", config[:headers[0].start()] if headers else config, {})]
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(config)
        fields = {}
        for line in config[header.end():end].splitlines():
            key, separator, value = line.partition("#")[0].partition("=")
            if separator and key.strip():
                fields.setdefault(key.strip().lower(), []).append(value.strip())
        sections.append(ConfigSection(header[1].strip().lower(), config[header.start():end], fields))
    return sections


def peer_sections(sections):
    peers = {}
    for section in sections:
        if section.kind != "peer":
            continue
        key = section.field("PublicKey")
        if not key or key in peers:
            raise ServerError("The AmneziaWG configuration contains a missing or duplicate peer key.")
        peers[key] = section
    return peers


def interface_section(sections):
    interfaces = [section for section in sections if section.kind == "interface"]
    if len(interfaces) != 1:
        raise ServerError("The AmneziaWG configuration must have one Interface section.")
    return interfaces[0]


def read_live_peers():
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", AWG_INTERFACE):
        raise ServerError("AWG_INTERFACE is not a valid AmneziaWG interface name.")
    output = docker_exec(
        "awg", "show", AWG_INTERFACE, "allowed-ips",
        error="Could not read the running AmneziaWG interface. Check AWG_CONTAINER and AWG_INTERFACE.",
    )
    peers = {}
    try:
        for line in output.splitlines():
            public_key, allowed_ips = line.split(None, 1)
            peers[public_key] = () if allowed_ips == "(none)" else tuple(
                str(ip_network(address, strict=False)) for address in allowed_ips.replace(",", " ").split()
            )
    except ValueError:
        raise ServerError("The running AmneziaWG interface returned an unsupported peer list.") from None
    return peers


def read_clients_table_names():
    if not AWG_CLIENTS_TABLE.startswith("/"):
        raise ServerError("AWG_CLIENTS_TABLE must be an absolute path inside the AmneziaWG container.")
    content = docker_exec(
        "sh", "-c", 'if [ -e "$1" ]; then cat "$1"; fi', "sh", AWG_CLIENTS_TABLE,
        error="Could not read the AmneziaWG clientsTable.",
    )
    if not content.strip():
        return {}
    try:
        table = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ServerError("The AmneziaWG clientsTable is not valid JSON.") from None

    if isinstance(table, dict):
        users = table.items()
    elif isinstance(table, list):
        users = (
            (entry.get("clientId"), entry.get("userData")) for entry in table if isinstance(entry, dict)
        )
    else:
        raise ServerError("The AmneziaWG clientsTable format is unsupported.")

    names = {}
    for public_key, user_data in users:
        if isinstance(public_key, str) and isinstance(user_data, dict):
            name = user_data.get("clientName")
            if isinstance(name, str) and name.strip():
                names[public_key] = name
    return names


def read_peer_comment_names(peers):
    names = {}
    for key, section in peers.items():
        name = re.search(r"(?im)^[ \t]*#_?[ \t]*Name[ \t]*=[ \t]*([^\r\n]+)", section.text)
        if name and name[1].strip():
            names[key] = name[1].strip()
    return names


def profile_names(persistent):
    names = read_clients_table_names()
    names.update(read_peer_comment_names(persistent))
    return names


def read_profiles(persistent, peers):
    names = profile_names(persistent)
    return [Profile(key, names.get(key), addresses) for key, addresses in peers.items()]


def list_profiles(_request):
    peers = read_live_peers()
    persistent = peer_sections(config_sections(read_config(config_path())))
    return {
        "profiles": [{"name": profile.name, "addresses": profile.addresses} for profile in read_profiles(persistent, peers)],
    }


def peer_networks(peer):
    return (
        ip_network(address.strip(), strict=False)
        for value in peer.fields.get("allowedips", [])
        for address in value.split(",")
        if address.strip()
    )


def available_address(interface, persistent, live):
    try:
        server_addresses = [
            ip_interface(address.strip())
            for value in interface.fields.get("address", [])
            for address in value.split(",")
        ]
        occupied = [
            ip_network(address, strict=False)
            for addresses in live.values()
            for address in addresses
        ]
        occupied.extend(network for peer in persistent.values() for network in peer_networks(peer))
    except ValueError:
        raise ServerError("Could not determine the configured AmneziaWG address pool.") from None

    occupied.extend(ip_network(str(address.ip)) for address in server_addresses)
    ipv4_ranges = sorted(
        (int(network.network_address), int(network.broadcast_address))
        for network in occupied
        if network.version == 4
    )
    for address in server_addresses:
        if address.version != 4:
            continue
        network = address.network
        first = int(network.network_address) + (network.prefixlen < 31)
        last = int(network.broadcast_address) - (network.prefixlen < 31)
        for start, end in ipv4_ranges:
            if start > first:
                break
            first = max(first, end + 1)
        if first <= last:
            return f"{IPv4Address(first)}/32"
    raise ServerError("The configured AmneziaWG IPv4 pool has no free client address.")


def udp_port(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = 0
    if not 1 <= result <= 65535:
        raise ServerError("Could not determine a valid AmneziaWG UDP port.")
    return result


def public_udp_port():
    if override := os.environ.get("AWG_PUBLIC_PORT"):
        return udp_port(override)
    listen_port = udp_port(docker_exec(
        "awg", "show", AWG_INTERFACE, "listen-port",
        error="Could not read the running AmneziaWG UDP port.",
    ))
    output = docker_command(
        "container", "inspect", "--format", "{{json .HostConfig.NetworkMode}}\n{{json .NetworkSettings.Ports}}",
        AWG_CONTAINER, error="Could not inspect Docker's AmneziaWG UDP port bindings.",
    )
    try:
        network_mode, ports = map(json.loads, output.splitlines())
        if network_mode == "host":
            return listen_port
        bindings = (ports or {}).get(f"{listen_port}/udp") or []
        published = {udp_port(binding["HostPort"]) for binding in bindings}
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ServerError("Could not read Docker's AmneziaWG UDP port bindings.") from None
    if len(published) != 1:
        raise ServerError("Set AWG_PUBLIC_PORT on the manager when Docker has no unique published AmneziaWG UDP port.")
    return published.pop()


def profile_endpoint(host):
    host = os.environ.get("AWG_PUBLIC_HOST") or host
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", host):
        raise ServerError("A VPS hostname or IP address is required for the profile endpoint.")
    try:
        address = ip_address(host)
    except ValueError:
        labels = host.removesuffix(".").split(".")
        if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
            raise ServerError("A valid VPS hostname or IP address is required for the profile endpoint.") from None
    else:
        host = f"[{address}]" if address.version == 6 else str(address)
    return f"{host}:{public_udp_port()}"


def awg_key(*args, input_text=None):
    key = docker_exec("awg", *args, input_text=input_text, error="Could not read or generate an AmneziaWG key.").strip()
    try:
        valid = len(base64.b64decode(key, validate=True)) == 32
    except ValueError:
        valid = False
    if not valid:
        raise ServerError("AmneziaWG returned an invalid key.")
    return key


def verified_server_public_key(interface):
    public_key = awg_key("show", AWG_INTERFACE, "public-key")
    if awg_key("pubkey", input_text=interface.field("PrivateKey") + "\n") != public_key:
        raise ServerError("The running and persistent AmneziaWG configurations have different server keys.")
    return public_key


def client_settings(interface):
    for key in AWG_INTERFACE_KEYS:
        value = interface.field(key)
        if not value and key in ("I1", "I2", "I3", "I4", "I5"):
            match = re.search(rf"(?im)^[ \t]*#[ \t]*{key}[ \t]*=[ \t]*([^#\r\n]+)", interface.text)
            value = match[1].strip() if match else ""
        if value:
            yield f"{key} = {value}"


def handshake_settings(interface):
    def effective_value(key, default):
        value = interface.field(key)
        return default if value.lower() in ("", "off", "(off)") else value

    paddings = tuple(int(effective_value(f"S{index}", "0")) for index in range(1, 5))
    headers = []
    for index in range(1, 5):
        first, _, last = effective_value(f"H{index}", str(index)).partition("-")
        headers.append((int(first), int(last or first)))
    protection_key = base64.b64decode(effective_value("HeaderProtectionKey", ""), validate=True)
    return paddings, headers, protection_key or bytes(32)


def verify_handshake_settings(interface, active_interface):
    try:
        identical = handshake_settings(interface) == handshake_settings(active_interface)
    except ValueError:
        raise ServerError("Could not compare the running and persistent AmneziaWG settings.") from None
    if not identical:
        raise ServerError("The running and persistent AmneziaWG handshake settings differ. Apply the server settings before creating a profile.")


def write_config(path, expected, config):
    expected_hash = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    new_hash = hashlib.sha256(config.encode("utf-8")).hexdigest()
    error = "Could not write the persistent AmneziaWG configuration."
    # The final write must keep the existing inode for single-file bind mounts.
    result = docker_mutation(
        "sh", "-c",
        """
        umask 077
        work=$(mktemp -d /tmp/awg-profile-manager.XXXXXXXXXX) || { echo not-written; exit; }
        keep_backup=
        trap 'rm -f "$work/next"; if [ -z "$keep_backup" ]; then rm -rf "$work"; fi' EXIT
        if ! cat >"$work/next" || ! staged=$(sha256sum <"$work/next") ||
           [ "${staged%% *}" != "$3" ] || ! cat "$1" >"$work/previous" ||
           ! previous=$(sha256sum <"$work/previous") || ! sync "$work/previous" "$work" /tmp ||
           ! current=$(sha256sum <"$1"); then
            echo not-written
            exit
        fi
        if [ "${previous%% *}" != "$2" ] || [ "${current%% *}" != "$2" ]; then
            echo changed
            exit
        fi
        keep_backup=1
        if cat "$work/next" >"$1" && sync "$1"; then
            keep_backup=
            echo updated
        else
            printf 'backup %s/previous\\n' "$work"
        fi
        """,
        "sh", path, expected_hash, new_hash, input_text=config, error=error,
    )
    if result == "updated":
        return
    if result == "changed":
        raise ServerError("The persistent configuration changed during the request. Run list before retrying.")
    if result == "not-written":
        raise ServerError(error)
    if re.fullmatch(r"backup /tmp/awg-profile-manager\.[A-Za-z0-9]+/previous", result):
        raise MutationUncertain(f"{error} A private copy of the previous configuration is in {result.split(' ', 1)[1]} inside the AmneziaWG container.")
    raise MutationUncertain(error)


def change_config_and_peer(path, old_config, new_config, apply_live, restore_live, verify_live):
    unknown = "The VPN state may have changed. Check the VPS before retrying."
    try:
        if new_config != old_config:
            write_config(path, old_config, new_config)
        elif read_config(path) != old_config:
            raise ServerError("The persistent configuration changed during the request. Run list before retrying.")
    except MutationUncertain as error:
        raise ServerError(f"{error} {unknown}") from None
    except ServerError as error:
        raise ServerError(f"{error} No changes were made by this request.") from None

    live_attempted = False
    try:
        if read_config(path) != new_config:
            raise ServerError("Could not verify the updated persistent configuration.")
        live_attempted = True
        apply_live()
        if not verify_live():
            raise ServerError("Could not verify the updated running AmneziaWG peer.")
        if read_config(path) != new_config:
            raise ServerError("The persistent configuration changed while updating the running peer.")
    except MutationUncertain as error:
        raise ServerError(f"{error} {unknown}") from None
    except ServerError as error:
        try:
            current = read_config(path)
            if current not in (new_config, old_config):
                raise ServerError("The persistent configuration has a different update.")
            if live_attempted:
                restore_live()
            if current != old_config:
                write_config(path, new_config, old_config)
            if read_config(path) != old_config:
                raise ServerError("Could not verify that the persistent configuration was restored.")
        except ServerError as rollback_error:
            raise ServerError(f"{error} {rollback_error} {unknown}") from None
        raise ServerError(f"{error} The previous VPN state was restored.") from None


def create_profile(request):
    name = request.get("name")
    if not valid_profile_name(name):
        raise ServerError(PROFILE_NAME_RULE)

    path = config_path()
    config = read_config(path)
    sections = config_sections(config)
    interface = interface_section(sections)
    if interface.field("SaveConfig").lower() == "true":
        raise ServerError("Creation requires SaveConfig to be disabled so restarts retain profile names.")
    persistent = peer_sections(sections)
    active_sections = read_live_sections()
    verify_handshake_settings(interface, interface_section(active_sections))
    live = {key: live_peer_addresses(peer) for key, peer in peer_sections(active_sections).items()}
    names = profile_names(persistent)
    if any(names.get(key) == name for key in live.keys() | persistent.keys()):
        raise ServerError("That profile name already exists on the VPS.")

    address = available_address(interface, persistent, live)
    endpoint = profile_endpoint(request.get("host"))
    server_public_key = verified_server_public_key(interface)
    private_key = awg_key("genkey")
    public_key = awg_key("pubkey", input_text=private_key + "\n")
    if public_key == server_public_key or public_key in live or public_key in persistent:
        raise ServerError("AmneziaWG generated a key already in use. Retry the command.")
    preshared_key = awg_key("genpsk")
    updated_config = "".join((
        config, "\n\n" if not config.endswith("\n") else "\n",
        f"[Peer]\n# Name = {name}\nPublicKey = {public_key}\n",
        f"PresharedKey = {preshared_key}\nAllowedIPs = {address}\n",
    ))
    client_config = "\n".join((
        "[Interface]", f"PrivateKey = {private_key}", f"Address = {address}",
        "DNS = 1.1.1.1, 1.0.0.1", *client_settings(interface),
        "", "[Peer]", f"PublicKey = {server_public_key}", f"PresharedKey = {preshared_key}",
        "AllowedIPs = 0.0.0.0/0, ::/0", f"Endpoint = {endpoint}", "PersistentKeepalive = 25", "",
    ))

    def add_live():
        mutate_peer(
            "set", AWG_INTERFACE, "peer", public_key,
            "preshared-key", "/dev/stdin", "allowed-ips", address,
            input_text=preshared_key + "\n", error="Could not add the running AmneziaWG peer.",
        )

    def undo_add():
        if public_key in read_live_peers():
            mutate_peer(
                "set", AWG_INTERFACE, "peer", public_key, "remove",
                error="Could not roll back the running AmneziaWG peer.",
            )
        if public_key in read_live_peers():
            raise ServerError("Could not verify the running AmneziaWG peer was rolled back.")

    change_config_and_peer(
        path, config, updated_config, add_live, undo_add,
        lambda: read_live_peers().get(public_key) == (address,),
    )
    return {"config": client_config}


def read_live_sections():
    config = docker_exec(
        "awg", "showconf", AWG_INTERFACE,
        error="Could not read the running AmneziaWG configuration.",
    )
    return config_sections(config)


def live_peer_addresses(peer):
    try:
        return tuple(str(network) for network in peer_networks(peer))
    except ValueError:
        raise ServerError("The running AmneziaWG configuration contains an invalid VPN address.") from None


def live_peer_fields(peer):
    # AmneziaWG learns a client's Endpoint as it connects and roams.
    fields = {key: value for key, value in peer.fields.items() if key not in ("endpoint", "allowedips")}
    fields["allowedips"] = sorted(live_peer_addresses(peer))
    return fields


def resolve_profile(request, profiles):
    name = request.get("name")
    address = request.get("address")
    if isinstance(name, str) and name and address is None:
        matches = [profile for profile in profiles if profile.name == name]
        label = name
    elif isinstance(address, str) and address and name is None:
        try:
            label = str(ip_network(address))
        except ValueError:
            raise ServerError("Enter a VPN address or the full CIDR shown by list.") from None
        matches = [profile for profile in profiles if profile.name is None and label in profile.addresses]
    else:
        raise ServerError("Choose a profile by name or an unnamed profile by VPN address.")
    if len(matches) != 1:
        raise ServerError("The selection must match exactly one VPN profile. Run list and select again.")
    return matches[0], label


def revoke_profile(request):
    try:
        path = config_path()
        config = read_config(path)
        sections = config_sections(config)
        persistent = peer_sections(sections)
        server_public_key = verified_server_public_key(interface_section(sections))
        live = peer_sections(read_live_sections())
        addresses = {key: live_peer_addresses(peer) for key, peer in live.items()}
        profile, label = resolve_profile(request, read_profiles(persistent, addresses))
        public_key = profile.public_key
        live_peer = live[public_key]
        previous_fields = live_peer_fields(live_peer)
        persistent_peer = persistent.get(public_key)
        identity = [server_public_key, label, previous_fields, persistent_peer.fields if persistent_peer else None]
        selection = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
        if request["action"] == "preview-revoke":
            return {"label": label, "selection": selection}
        if request.get("selection") != selection:
            raise ServerError("The selected VPN profile changed. Run list and select again.")
    except ServerError as error:
        raise ServerError(f"{error} No changes were made.") from None

    updated_config = "".join(section.text for section in sections if section is not persistent_peer)

    def remove_live():
        mutate_peer(
            "set", AWG_INTERFACE, "peer", public_key, "remove",
            error="Could not revoke the running AmneziaWG peer.",
        )

    def restore_live():
        current = peer_sections(read_live_sections()).get(public_key)
        if current is not None and live_peer_fields(current) == previous_fields:
            return
        mutate_peer(
            "addconf", AWG_INTERFACE, "/dev/stdin", input_text=live_peer.text,
            error="Could not restore the running AmneziaWG peer.",
        )
        restored = peer_sections(read_live_sections()).get(public_key)
        if restored is None or live_peer_fields(restored) != previous_fields:
            raise ServerError("Could not verify that the running AmneziaWG peer was restored.")

    change_config_and_peer(
        path, config, updated_config, remove_live, restore_live,
        lambda: public_key not in read_live_peers(),
    )
    return {}


OPERATIONS = {
    "list": list_profiles,
    "create": create_profile,
    "preview-revoke": revoke_profile,
    "revoke": revoke_profile,
}


def main():
    try:
        raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ServerError("The request is too large.")
        request = json.loads(raw)
        action = request.get("action") if isinstance(request, dict) else None
        if not isinstance(action, str) or action not in OPERATIONS:
            raise ServerError("Unknown profile command.")
        with open(LOCK_PATH, "a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            reply = {"ok": True, **OPERATIONS[action](request)}
    except (json.JSONDecodeError, UnicodeDecodeError):
        reply = {"ok": False, "error": "The request is not valid JSON."}
    except ServerError as error:
        reply = {"ok": False, "error": str(error)}
    except OSError:
        reply = {"ok": False, "error": "Could not lock the AmneziaWG profile manager."}
    print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
