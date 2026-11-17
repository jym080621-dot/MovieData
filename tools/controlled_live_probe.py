"""Bounded read-only live check. Never run by unittest discovery.

At most 8 GETs, >=5s between starts, no retries or redirect following.
Stops on any transport/payload error or HTTP denial; no national scan.
"""
import argparse
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import socket
import sys
import time
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zgdypf.client import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    entries = []
    try:
        addresses = sorted({r[4][0] for r in socket.getaddrinfo('zgdypf.zgdypw.cn', 443)})
        dns = dict(ok=True, addresses=addresses)
    except OSError as exc:
        dns = dict(ok=False, error=str(exc))
    client = Client(rate=0.2, jitter=(0, 0), retries=1, timeout=20, request_guard=lambda: None)
    original = client._open
    last_start = None

    def recorded(req, timeout):
        nonlocal last_start
        if len(entries) >= 8:
            raise RuntimeError('live request budget exhausted')
        if last_start is not None:
            time.sleep(max(0, 5 - (time.monotonic() - last_start)))
        last_start = time.monotonic()
        entry = dict(index=len(entries)+1, url=req.full_url,
                     started_at=dt.datetime.now(dt.timezone.utc).isoformat())
        entries.append(entry)
        try:
            with original(req, timeout=timeout) as response:
                body = response.read()
                entry.update(status=response.status, headers={k: response.headers[k] for k in
                             ('Content-Type', 'Content-Length', 'Date', 'Retry-After') if k in response.headers},
                             tls_verified=True, bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
            path = out / f'{entry["index"]:02d}_response.bin'
            path.write_bytes(body)
            entry['body_file'] = path.name
            result = io.BytesIO(body)
            result.status = entry['status']
            return result
        except urllib.error.HTTPError as exc:
            body = exc.read()
            (out / f'{entry["index"]:02d}_response.bin').write_bytes(body)
            entry.update(status=exc.code, error=str(exc), bytes=len(body),
                         headers={k: exc.headers[k] for k in ('Content-Type', 'Date', 'Retry-After') if k in exc.headers})
            raise
        except Exception as exc:
            entry['error'] = str(exc)
            raise
        finally:
            entry['elapsed_ms'] = round((time.monotonic()-last_start)*1000)
            print(json.dumps(entry, ensure_ascii=False), flush=True)

    client._open = recorded
    stopped = None
    try:
        client.fetch('/getDateRange')
        for day in ('2026-04-30', '2026-11-12'):
            response = client.fetch('/getCinemaList', dict(date=day, withSvcFee='true', limit=2, offset=0))
            rows = response.data.get('salesRankList') if isinstance(response.data, dict) else None
            if not rows:
                continue
            code = rows[0]['code']
            response = client.fetch('/getCinemaBoxData', dict(cinemaCode=code, date=day))
            if response.status == 204:
                client.fetch('/getCinemaSumData', dict(cinemaCode=code, date=day))
        client.fetch('/searchMovie', dict(movieName='哪吒', limit=2, offset=0))
    except Exception as exc:
        stopped = f'{type(exc).__name__}: {exc}'
    report = dict(dns=dns, max_requests=8, interval_seconds=5, retries=0,
                  redirects_followed=False, requests=len(entries), stopped=stopped, entries=entries)
    (out / 'live_results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if stopped is None else 2


if __name__ == '__main__':
    raise SystemExit(main())
