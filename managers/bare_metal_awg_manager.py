"""
BareMetalAWGManager — drives the valujin/amneziawg-installer scripts over SSH.

Unlike AWGManager (Docker-based), this manager treats AmneziaWG 2.0 as a
host-level service installed by install_amneziawg.sh and managed by
manage_amneziawg.sh. It exposes the same surface used by the rest of the
panel (check_protocol_installed, install_protocol, get_clients, add_client,
remove_client, get_client_config, get_server_status, etc.) so it can be
plugged in via get_protocol_manager() without changes elsewhere.

Server-side layout assumed (mirrors the installer's defaults):
  /root/awg/install_amneziawg.sh       — installer
  /root/awg/manage_amneziawg.sh        — peer/state management
  /root/awg/<name>.conf                — client config (one per peer)
  /root/awg/<name>.vpnuri              — vpn:// URI (one per peer)
  /etc/amnezia/amneziawg/awg0.conf     — live server config
  /root/awg/awgsetup_cfg.init          — bootstrap parameters
"""

import logging
import os
import re
import shlex

logger = logging.getLogger(__name__)


# Defaults match the panel's preferred bare-metal flags, requested by the
# user: 9443 / route-all / yes / no-reboot / allow-ipv6 / preset=custom-conf
# with the canonical conf file pinned to main on GitHub.
DEFAULT_INSTALL_OPTIONS = {
    'port': '9443',
    'routing_mode': 'route-all',
    'allow_ipv6': True,
    'no_reboot': True,
    'auto_yes': True,
    'preset': 'custom-conf',
    'conf': 'https://raw.githubusercontent.com/valujin/amneziawg-installer/main/conf/amneziawg-2.0-1779689104577.conf',
    'no_tweaks': False,
    'subnet': '',
    'endpoint': '',
    'custom_routes': '',
}

INSTALLER_URL = 'https://raw.githubusercontent.com/valujin/amneziawg-installer/main/install_amneziawg.sh'


def merge_install_options(user_opts):
    """Overlay user-provided install options on top of the panel defaults."""
    merged = dict(DEFAULT_INSTALL_OPTIONS)
    if user_opts:
        for k, v in user_opts.items():
            if v is None:
                continue
            merged[k] = v
    return merged


class BareMetalAWGManager:
    """AmneziaWG 2.0 driver for native (non-Docker) installs via SSH."""

    # Protocol identifiers — only AWG2 is supported on bare-metal servers.
    AWG2 = 'awg2'

    AWG_DIR = '/root/awg'
    SERVER_CONF = '/etc/amnezia/amneziawg/awg0.conf'
    MANAGE_PATH = '/root/awg/manage_amneziawg.sh'
    INSTALLER_PATH = '/root/awg/install_amneziawg.sh'

    def __init__(self, ssh_manager):
        self.ssh = ssh_manager

    # ----- compatibility shims used by check_server() and friends -----

    def check_docker_installed(self):
        """Bare-metal installs do not require Docker."""
        return False

    def check_protocol_installed(self, protocol_type):
        if protocol_type != self.AWG2:
            return False
        out, _, _ = self.ssh.run_sudo_command(
            f"test -f {self.SERVER_CONF} && echo OK || true"
        )
        return 'OK' in (out or '')

    def check_container_running(self, protocol_type):
        """For bare-metal: equivalent to systemd unit being active."""
        if protocol_type != self.AWG2:
            return False
        out, _, _ = self.ssh.run_sudo_command(
            "systemctl is-active awg-quick@awg0 2>/dev/null || true"
        )
        first = (out or '').strip().split('\n', 1)[0]
        return first == 'active'

    # ===================== INSTALL / UNINSTALL =====================

    def install_protocol(self, protocol_type, port=None, awg_params=None,
                         install_options=None):
        """Run install_amneziawg.sh on the remote host with CLI flags.

        `install_options` is a dict matching the keys in
        DEFAULT_INSTALL_OPTIONS. `port` overrides install_options['port']
        when provided.
        """
        if protocol_type != self.AWG2:
            raise RuntimeError(
                f"BareMetalAWGManager supports only awg2 (got {protocol_type})"
            )

        opts = merge_install_options(install_options)
        if port:
            opts['port'] = str(port)

        flags = self._build_install_flags(opts)

        # Download installer fresh on each run so the remote stays at HEAD.
        # `set +e` after curl so we can capture installer exit code separately.
        script = (
            "set -e\n"
            f"mkdir -p {self.AWG_DIR}\n"
            f"curl -fsSL --retry 2 --connect-timeout 15 --max-time 60 "
            f"-o {self.INSTALLER_PATH} {INSTALLER_URL}\n"
            f"chmod +x {self.INSTALLER_PATH}\n"
            f"bash {self.INSTALLER_PATH} {flags}\n"
        )

        out, err, code = self.ssh.run_sudo_script(script, timeout=1800)
        if code != 0:
            raise RuntimeError(
                f"install_amneziawg.sh failed (rc={code}): "
                f"{(err or out or '')[-2000:]}"
            )

        return {
            'status': 'success',
            'port': str(opts.get('port', '9443')),
            'awg_params': self._read_awg_params(),
            'output_tail': (out or '')[-2000:],
        }

    def _build_install_flags(self, opts):
        """Translate option dict into safe CLI flags."""
        flags = []
        if opts.get('auto_yes', True):
            flags.append('--yes')
        if opts.get('port'):
            flags.append(f"--port={opts['port']}")
        if opts.get('subnet'):
            flags.append(f"--subnet={opts['subnet']}")

        routing = opts.get('routing_mode') or 'route-all'
        if routing == 'route-all':
            flags.append('--route-all')
        elif routing == 'route-amnezia':
            flags.append('--route-amnezia')
        elif routing == 'route-custom':
            routes = (opts.get('custom_routes') or '').strip()
            if routes:
                flags.append(f"--route-custom={routes}")

        if opts.get('allow_ipv6', True):
            flags.append('--allow-ipv6')
        else:
            flags.append('--disallow-ipv6')

        preset = opts.get('preset') or 'custom-conf'
        flags.append(f"--preset={preset}")
        if preset == 'custom-conf':
            conf = opts.get('conf') or DEFAULT_INSTALL_OPTIONS['conf']
            flags.append(f"--conf={conf}")

        if opts.get('no_reboot', True):
            flags.append('--no-reboot')
        if opts.get('no_tweaks'):
            flags.append('--no-tweaks')
        if opts.get('endpoint'):
            flags.append(f"--endpoint={opts['endpoint']}")

        # shlex.quote each flag so URLs / subnets with shell metacharacters
        # cannot escape the installer invocation.
        return ' '.join(shlex.quote(f) for f in flags)

    def uninstall_protocol(self, protocol_type):
        if protocol_type != self.AWG2:
            raise RuntimeError(
                f"BareMetalAWGManager supports only awg2 (got {protocol_type})"
            )
        out, err, code = self.ssh.run_sudo_command(
            f"bash {self.INSTALLER_PATH} --uninstall --yes", timeout=600
        )
        if code != 0 and 'не установлен' not in (err or '') and 'not installed' not in (err or ''):
            raise RuntimeError(f"Uninstall failed: {err or out}")
        return {'status': 'success'}

    # ===================== STATUS =====================

    def get_server_status(self, protocol_type):
        """Match AWGManager.get_server_status() shape."""
        info = {
            'protocol': protocol_type,
            'container_exists': False,
            'container_running': False,
        }
        if protocol_type != self.AWG2:
            return info

        info['container_exists'] = self.check_protocol_installed(protocol_type)
        if not info['container_exists']:
            return info

        info['container_running'] = self.check_container_running(protocol_type)
        try:
            info['port'] = self._read_listen_port() or ''
            info['awg_params'] = self._read_awg_params()
            info['clients_count'] = len(self._parse_peers())
        except Exception as e:  # noqa: BLE001 — telemetry only
            info['error'] = str(e)
        return info

    # ===================== CLIENTS =====================

    def get_clients(self, protocol_type):
        """Return list of clients in AWGManager-compatible shape.

        The panel UI (server.html `loadConnections`) reads display fields
        from `client.userData`, not from the top-level dict. We mirror the
        Docker AWGManager output so the existing renderer Just Works:

            {clientId, enabled, userData: {clientName, clientIp,
             latestHandshake, dataReceived, dataSent,
             dataReceivedBytes, dataSentBytes, allowedIps}}

        Live traffic comes from `awg show awg0` (AmneziaWG user-space tool,
        same line format as `wg show`).
        """
        if protocol_type != self.AWG2:
            return []
        peers = self._parse_peers()
        try:
            live = self._awg_show()
        except Exception as e:  # noqa: BLE001 — telemetry only
            logger.warning('awg show failed: %s', e)
            live = {}

        results = []
        for p in peers:
            pub = p.get('public_key', '')
            show = live.get(pub, {})
            allowed = p.get('allowed_ips', '') or show.get('allowedIps', '')
            client_ip = (allowed.split(',')[0].strip().split('/')[0]
                         if allowed else '')
            name = p.get('name') or (
                f'External ({client_ip})' if client_ip else 'External (native)'
            )
            # The installer stores per-client private keys in
            # `<AWG_DIR>/<name>.conf`. We don't ship the key in the listing
            # (UI only needs a truthy presence marker — the key is fetched on
            # demand via get_client_config), but flagging it lets the UI show
            # the "view config" button instead of the "Configuration
            # unavailable" warning, which is reserved for peers created via
            # the native Amnezia mobile app (those have no #_Name marker).
            has_internal_key = bool(p.get('name'))
            results.append({
                'clientId': pub,
                'enabled': True,
                'userData': {
                    'clientName': name,
                    'clientIp': client_ip,
                    'clientPrivateKey': 'stored' if has_internal_key else '',
                    'clientPubKey': pub,
                    'allowedIps': allowed,
                    'latestHandshake': show.get('latestHandshake', ''),
                    'dataReceived': show.get('dataReceived', ''),
                    'dataSent': show.get('dataSent', ''),
                    'dataReceivedBytes': show.get('dataReceivedBytes', 0),
                    'dataSentBytes': show.get('dataSentBytes', 0),
                    'enabled': True,
                    'externalClient': not has_internal_key,
                },
            })
        return results

    def add_client(self, protocol_type, client_name, server_host, port):
        if protocol_type != self.AWG2:
            raise RuntimeError("Only awg2 is supported on native servers")

        safe_name = self._safe_client_name(client_name)
        if not safe_name:
            raise RuntimeError("Invalid client name")

        # manage_amneziawg.sh respects AWG_YES=1 for non-interactive runs.
        out, err, code = self.ssh.run_sudo_command(
            f"AWG_YES=1 bash {shlex.quote(self.MANAGE_PATH)} add {shlex.quote(safe_name)}",
            timeout=180,
        )
        if code != 0:
            raise RuntimeError(f"manage add failed: {(err or out or '')[-1000:]}")

        conf_remote = f"{self.AWG_DIR}/{safe_name}.conf"
        config_out, _, ccode = self.ssh.run_sudo_command(
            f"cat {shlex.quote(conf_remote)}"
        )
        if ccode != 0 or not config_out:
            raise RuntimeError(
                f"Client config not found at {conf_remote} after add"
            )

        pub = self._find_peer_pubkey_by_name(safe_name)
        client_ip = self._extract_client_ip(config_out)

        return {
            'client_name': safe_name,
            'client_id': pub or safe_name,
            'client_ip': client_ip,
            'config': config_out,
        }

    def remove_client(self, protocol_type, client_id):
        if protocol_type != self.AWG2:
            raise RuntimeError("Only awg2 is supported on native servers")

        # client_id may be either the safe_name (added via this manager)
        # or a public key (legacy entries). Resolve to a name either way.
        name = client_id
        peer = self._find_peer(client_id)
        if peer:
            name = peer.get('name') or client_id

        out, err, code = self.ssh.run_sudo_command(
            f"AWG_YES=1 bash {shlex.quote(self.MANAGE_PATH)} remove {shlex.quote(name)}",
            timeout=120,
        )
        if code != 0:
            raise RuntimeError(f"manage remove failed: {(err or out or '')[-1000:]}")
        return True

    def get_client_config(self, protocol_type, client_id, server_host, port):
        if protocol_type != self.AWG2:
            raise RuntimeError("Only awg2 is supported on native servers")

        peer = self._find_peer(client_id)
        name = peer.get('name') if peer else client_id
        conf_remote = f"{self.AWG_DIR}/{name}.conf"
        out, _, code = self.ssh.run_sudo_command(
            f"cat {shlex.quote(conf_remote)}"
        )
        if code != 0 or not out:
            raise RuntimeError(f"Client config not found: {conf_remote}")
        return out

    # ===================== BACKUPS =====================

    def create_backup_snapshot(self, local_dir, keep=10):
        """Create a server-side backup tarball and pull a copy locally.

        Steps (all idempotent and best-effort — never raises into the caller):
        1. Run `manage_amneziawg.sh backup`, which writes a timestamped
           tar.gz into `/etc/amnezia/amneziawg/backups/` covering the server
           config, peer configs, key material and the JSON metadata file.
        2. SFTP-download the newest tarball into `local_dir`.
        3. Prune the local mirror to `keep` files (newest first).

        Returns the absolute local path of the saved snapshot on success,
        or None when no snapshot could be produced. The remote manage
        script handles its own retention; we keep our local mirror small
        to bound panel disk usage.
        """
        try:
            os.makedirs(local_dir, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning('Backup: cannot create %s: %s', local_dir, e)
            return None

        # 1. Trigger remote backup.
        out, err, code = self.ssh.run_sudo_command(
            f"AWG_YES=1 bash {shlex.quote(self.MANAGE_PATH)} backup",
            timeout=120,
        )
        if code != 0:
            logger.warning(
                'Backup: remote manage backup failed (%s): %s',
                code, (err or out or '')[-400:],
            )
            return None

        # 2. Locate newest tarball. We use `ls -t` over `find -printf` so
        # this works on BusyBox-flavoured shells too.
        list_out, _, lcode = self.ssh.run_sudo_command(
            f"ls -1t {shlex.quote(self.AWG_DIR)}/backups/awg_backup_*.tar.gz 2>/dev/null | head -n1"
        )
        remote_path = (list_out or '').strip().splitlines()[0] if list_out else ''
        if lcode != 0 or not remote_path:
            logger.warning('Backup: cannot locate latest backup tarball')
            return None

        basename = os.path.basename(remote_path)
        local_path = os.path.join(local_dir, basename)

        # 3. Pull the tarball. Backups live under 0700/root, so stage to
        # /tmp via sudo first, then SFTP the world-readable copy.
        staged = f"/tmp/{basename}"
        _, _, scode = self.ssh.run_sudo_command(
            f"cp {shlex.quote(remote_path)} {shlex.quote(staged)} && "
            f"chmod 644 {shlex.quote(staged)}"
        )
        if scode != 0:
            logger.warning('Backup: cannot stage tarball at %s', staged)
            return None
        try:
            self.ssh.download_binary(staged, local_path)
        except Exception as e:  # noqa: BLE001
            logger.warning('Backup: SFTP download failed: %s', e)
            local_path = None
        finally:
            self.ssh.run_sudo_command(f"rm -f {shlex.quote(staged)}")

        if not local_path:
            return None

        # 4. Local retention — keep newest `keep`.
        try:
            entries = sorted(
                (e for e in os.listdir(local_dir)
                 if e.startswith('awg_backup_') and e.endswith('.tar.gz')),
                reverse=True,
            )
            for stale in entries[keep:]:
                try:
                    os.remove(os.path.join(local_dir, stale))
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass

        logger.info('Backup snapshot saved: %s', local_path)
        return local_path

    def toggle_client(self, protocol_type, client_id, enable):
        # The bare-metal manage script does not expose enable/disable —
        # only add / remove. We surface this clearly rather than silently
        # noop'ing, so the panel can show an actionable error.
        raise NotImplementedError(
            "Toggling clients is not supported on native AWG installs. "
            "Use remove + re-add to revoke access."
        )

    # ===================== INTERNALS =====================

    # ===================== INTERNALS =====================

    def _awg_show(self):
        """Run `awg show awg0` on the remote and return per-peer live data.

        Output format (one block per peer, identical to `wg show`):
            peer: <PublicKey>
              endpoint: ...
              allowed ips: 10.8.0.2/32
              latest handshake: 12 seconds ago
              transfer: 1.23 MiB received, 456 KiB sent
              persistent keepalive: every 25 seconds
        """
        out, _, code = self.ssh.run_sudo_command(
            "awg show awg0 2>/dev/null || true"
        )
        if code != 0 or not (out or '').strip():
            return {}
        result = {}
        current = None
        for raw in out.split('\n'):
            line = raw.strip()
            if line.startswith('peer:'):
                current = line.split(':', 1)[1].strip()
                result[current] = {}
                continue
            if not current or ':' not in line:
                continue
            key, _, value = line.partition(':')
            key = key.strip().lower()
            value = value.strip()
            if key == 'latest handshake':
                result[current]['latestHandshake'] = value
            elif key == 'allowed ips':
                result[current]['allowedIps'] = value
            elif key == 'endpoint':
                result[current]['endpoint'] = value
            elif key == 'transfer':
                # "1.23 MiB received, 456 KiB sent"
                parts = value.split(',')
                if len(parts) == 2:
                    received = parts[0].strip().replace(' received', '')
                    sent = parts[1].strip().replace(' sent', '')
                    result[current]['dataReceived'] = received
                    result[current]['dataSent'] = sent
                    result[current]['dataReceivedBytes'] = self._parse_bytes(received)
                    result[current]['dataSentBytes'] = self._parse_bytes(sent)
        return result

    def _parse_bytes(self, size_str):
        """Parse human-readable size like '1.50 MiB' / '512 B' into bytes."""
        try:
            parts = (size_str or '').strip().split()
            if len(parts) != 2:
                return 0
            val = float(parts[0])
            units = {
                'B': 1,
                'KiB': 1024,
                'MiB': 1024 ** 2,
                'GiB': 1024 ** 3,
                'TiB': 1024 ** 4,
            }
            return int(val * units.get(parts[1], 1))
        except Exception:
            return 0

    def _safe_client_name(self, name):
        cleaned = re.sub(r'[^A-Za-z0-9_.\-]', '_', (name or '').strip())
        cleaned = cleaned.strip('._-') or 'client'
        return cleaned[:32]

    def _read_server_conf(self):
        """Read the live server config with sudo. Returns '' if absent."""
        out, _, code = self.ssh.run_sudo_command(f"cat {self.SERVER_CONF}")
        if code != 0:
            return ''
        return out or ''

    def _read_listen_port(self):
        for line in self._read_server_conf().splitlines():
            line = line.strip()
            if line.lower().startswith('listenport'):
                _, _, val = line.partition('=')
                return val.strip()
        return ''

    def _read_awg_params(self):
        """Extract Jc/Jmin/Jmax/S1-S4/H1-H4/I1-I5 from [Interface] section."""
        text = self._read_server_conf()
        params = {}
        in_iface = False
        key_map = {
            'jc': 'junk_packet_count',
            'jmin': 'junk_packet_min_size',
            'jmax': 'junk_packet_max_size',
            's1': 'init_packet_junk_size',
            's2': 'response_packet_junk_size',
            's3': 'cookie_reply_packet_junk_size',
            's4': 'transport_packet_junk_size',
            'h1': 'init_packet_magic_header',
            'h2': 'response_packet_magic_header',
            'h3': 'underload_packet_magic_header',
            'h4': 'transport_packet_magic_header',
            'i1': 'i1', 'i2': 'i2', 'i3': 'i3', 'i4': 'i4', 'i5': 'i5',
            'listenport': 'port',
        }
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith('[Interface]'):
                in_iface = True
                continue
            if stripped.startswith('['):
                in_iface = False
                continue
            if not in_iface or not stripped or stripped.startswith('#'):
                continue
            if '=' not in stripped:
                continue
            k, _, v = stripped.partition('=')
            k = k.strip().lower()
            v = v.strip()
            mapped = key_map.get(k)
            if mapped:
                params[mapped] = v
        return params

    def _parse_peers(self):
        """Walk awg0.conf returning [{name, public_key, allowed_ips}, ...].

        The installer marks each peer with a `#_Name = ...` comment line in
        the same [Peer] block; we use that as the canonical client name.
        """
        text = self._read_server_conf()
        peers = []
        current = None
        in_peer = False
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith('[Peer]'):
                if current is not None:
                    peers.append(current)
                current = {'name': '', 'public_key': '', 'allowed_ips': ''}
                in_peer = True
                continue
            if line.startswith('['):
                if current is not None:
                    peers.append(current)
                    current = None
                in_peer = False
                continue
            if not in_peer or current is None:
                continue
            if line.startswith('#_Name'):
                _, _, name = line.partition('=')
                current['name'] = name.strip()
                continue
            if line.startswith('#') or not line:
                continue
            if '=' not in line:
                continue
            k, _, v = line.partition('=')
            k = k.strip().lower()
            v = v.strip()
            if k == 'publickey':
                current['public_key'] = v
            elif k == 'allowedips':
                current['allowed_ips'] = v
        if current is not None:
            peers.append(current)
        # Filter out empty peer blocks (only Address etc., no PublicKey)
        return [p for p in peers if p.get('public_key')]

    def _find_peer(self, client_id):
        """Look up a peer by either name or public key."""
        for p in self._parse_peers():
            if p.get('public_key') == client_id or p.get('name') == client_id:
                return p
        return None

    def _find_peer_pubkey_by_name(self, name):
        peer = self._find_peer(name)
        return peer.get('public_key') if peer else ''

    def _extract_client_ip(self, client_conf_text):
        for line in client_conf_text.splitlines():
            line = line.strip()
            if line.lower().startswith('address'):
                _, _, v = line.partition('=')
                return v.strip().split('/')[0].split(',')[0].strip()
        return ''
