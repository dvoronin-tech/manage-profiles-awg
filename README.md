# AmneziaWG Profile Manager

Manage profiles on a VPS where AmneziaWG already runs in Docker. The Mac client uses your existing SSH access. The server container opens no management port.

## Set up the VPS

The SSH account needs permission to run Docker directly. Access to the Docker socket gives the manager administrative access to the host. Only run an image you trust.

From this repository, build the server image on the VPS.

```sh
docker build -f Dockerfile.server -t awg-profile-server:local .
SERVER_IMAGE=awg-profile-server:local
```

Once the image is available in a registry, set `SERVER_IMAGE` to its published name and pull it instead.

```sh
SERVER_IMAGE=your-registry/your-account/awg-profile-server:your-tag
docker pull "$SERVER_IMAGE"
```

Start the long-running container.

```sh
docker run -d --restart unless-stopped --name awg-profile-manager \
  --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
  "$SERVER_IMAGE"
```

The manager defaults to VPN container `amnezia-awg` and interface `awg0`. If your installation uses `amnezia-awg2`, add `-e AWG_CONTAINER=amnezia-awg2` before the image name. You can also set `AWG_INTERFACE`, `AWG_CONFIG_PATH` and `AWG_CLIENTS_TABLE` in the same way. The paths refer to files inside the VPN container. The default config search order is `/etc/amneziawg/awg0.conf` followed by `/opt/amnezia/awg/awg0.conf`. The default names file is `/opt/amnezia/awg/clientsTable`.

Run one manager per VPN interface. Finish any direct changes through AmneziaVPN or a VPS editor before using this tool to create or revoke profiles. An interrupted configuration write can leave a private snapshot at `/tmp/awg-profile-manager.*/previous` inside the VPN container. It contains the configuration as it was before the write. Compare it with the current file before restoring it.

## Run the client on your Mac

Start Docker Desktop and build the client image from this repository.

```sh
docker build -f Dockerfile.client -t awg-profile-client:local .
```

The client uses your Mac's `~/.ssh/known_hosts`. If this is the first connection, compare the VPS host-key fingerprint with the one in your VPS provider's console, then save it using your normal SSH command. Use the same hostname or IP and port when you run this tool. Set `AWG_KNOWN_HOSTS` to use a different known-hosts file.

Load your key into the Mac's SSH agent or set `AWG_SSH_KEY` to the path of an unencrypted identity file. The launcher mounts the SSH agent or the chosen key file for use inside the client.

```sh
ssh-add ~/.ssh/id_ed25519
./awg-profiles --host vps.example.com --user root list
AWG_SSH_KEY="$HOME/.ssh/vps_key" ./awg-profiles --host vps.example.com --user root --port 2222 list
```

Use `--password` for a hidden terminal prompt if key authentication fails.

```sh
./awg-profiles --host vps.example.com --user root --password list
```

For redirected input, use `--password-stdin` with a password source that supplies one line. Both options verify the VPS host key and try SSH keys first. They read the password only if a key has not authenticated. Without either option, key failure ends the command. The VPS SSH service can accept a password directly or ask for it with a single hidden keyboard-interactive prompt.

The list shows each active peer, including profiles created before installation of the manager. When a peer has a saved name, the list shows it alongside the VPN address. A peer with no saved name appears as `(unnamed)`.

## Create a profile

```sh
./awg-profiles --host vps.example.com --user root create "My phone"
```

The client saves `My phone.conf` beside `awg-profiles` and prints its full path. If the local file or the profile name on the VPS already exists, the command stops with an error. Treat the file as a private credential. For an AmneziaWG 3.1 server, import it into a compatible client such as AmneziaVPN 5.0.1.5 or later. [Amnezia client compatibility](https://docs.amnezia.org/faq/#how-do-i-connect-using-amneziawg-31)

New profiles use an unused IPv4 address from the server's configured subnet and the VPS hostname with the VPN's published UDP port. If NAT or split DNS gives the VPN a different public endpoint, set `AWG_PUBLIC_HOST` and/or `AWG_PUBLIC_PORT` on the manager when starting it. The manager requires `SaveConfig` to be disabled for creation so the persistent configuration keeps the profile name.

## Revoke a profile

Use `list` to find the profile. Select a named profile by its exact name, or select an unnamed profile by its VPN address.

```sh
./awg-profiles --host vps.example.com --user root revoke --name "My phone"
./awg-profiles --host vps.example.com --user root revoke --address 10.8.1.2
```

For an unnamed profile with a network instead of a single address, pass the full CIDR shown by `list`. Run the command in a terminal and use `--password` if you need password fallback. The prompt shows the selected profile and the VPS's SSH address and port. Enter `y` or `yes` to revoke access. Press Enter to cancel. If the selected profile changed, run `list` and select it again. After confirmation and successful revocation, any copy of that profile's configuration loses access immediately and remains revoked after AmneziaWG restarts.
