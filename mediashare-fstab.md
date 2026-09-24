# MediaShare CIFS mount

Reference for re-creating the media share mount on a fresh install (e.g. the
Gentoo switch). Server is the same box at `192.168.1.34`, reached over the
`wg0` WireGuard tunnel.

## fstab line

```
//192.168.1.34/MediaShare  /mnt/mediashare  cifs  credentials=/etc/samba/creds-mediashare,uid=1000,gid=1000,_netdev,noauto,x-systemd.automount,x-systemd.device-timeout=10s,x-systemd.idle-timeout=60,x-systemd.requires=wg-quick@wg0.service,x-systemd.after=wg-quick@wg0.service,soft,noserverino  0  0
```

- `noauto,x-systemd.automount` — doesn't mount at boot, only on first
  access. No boot-time hang if the server/tunnel is down.
- `x-systemd.requires`/`after=wg-quick@wg0.service` — waits for the
  WireGuard tunnel before attempting the mount.
- `soft,noserverino` — avoids hard-lockups if the share drops mid-use.

## Credentials file

`/etc/samba/creds-mediashare`, mode `600`, owned by root. **Not committed
here** — grab the real values from the current machine before wiping it
(`sudo cat /etc/samba/creds-mediashare`) and recreate manually:

```
username=<user>
password=<password>
```

## Share layout

- `/mnt/mediashare/photos`, `/mnt/mediashare/videos` — plain file dumps,
  wired up as Immich External Library import paths.
- `/mnt/mediashare/immich/` — Immich's own internal data dir (library,
  upload, thumbs, etc). Don't point anything else at this.
- `videos/` had a server-side write ACL issue as of 2026-09-24 (writes
  silently fail/disappear even with rsync `--inplace`) — needs the NAS
  side fixed before it's usable again.
