#!/usr/bin/env python3
"""res.json files -> results.csv (one row per benchmark x runtime, median of rounds) + summary print."""
import csv, json, statistics, sys

META = [
    # id, label, section, unit, better, silkjs on 2026-11-11 (start of the Bun thread)
    ('serve', 'HTTP hello, native serve (Bun.serve / Deno.serve / silk:serve)', 'HTTP', 'req/s', 'higher', ''),
    ('node_http', 'HTTP hello, node:http', 'HTTP', 'req/s', 'higher', 2350),
    ('express_https', 'Express hello over HTTPS', 'HTTP', 'req/s', 'higher', 950),
    ('ws_bun_publish', 'WebSocket chat, Bun publish API', 'WebSocket', 'msg/s', 'higher', ''),
    ('ws_deno', 'WebSocket chat, Deno.upgradeWebSocket', 'WebSocket', 'msg/s', 'higher', ''),
    ('ws_api', 'WebSocket chat, ws package API', 'WebSocket', 'msg/s', 'higher', 0),
    ('postgres', 'Postgres, 100k x SELECT 100 rows', 'Database', 'ms', 'lower', ''),
    ('sqlite_order', 'SQLite SELECT * FROM "Order" (16,818 rows)', 'Database', 'ms', 'lower', 218.9),
    ('sqlite_product', 'SQLite SELECT * FROM "Product" (77 rows)', 'Database', 'ms', 'lower', 0.671),
    ('sqlite_orderdetail', 'SQLite SELECT * FROM "OrderDetail" (621,883 rows)', 'Database', 'ms', 'lower', 18931),
    ('sqlite_exec8000', 'SQLite db.exec, 8000 statements', 'Database', 'ms', 'lower', 5.7),
    ('spawnSync_node_api', 'spawnSync("echo", ["hi"]), node API', 'Process and I/O', 'us', 'lower', 3211),
    ('spawnSync_bun', 'Bun.spawnSync echo hi', 'Process and I/O', 'us', 'lower', ''),
    ('cat_streams', 'cat 1 GB file to /dev/null, fs streams', 'Process and I/O', 'ms', 'lower', 7175),
    ('cat_bun_write', 'cat 1 GB file to /dev/null, Bun.write', 'Process and I/O', 'ms', 'lower', ''),
    ('setImmediate', 'setImmediate turns per second', 'Process and I/O', 'turns/s', 'higher', 24000),
]
SQL = {'SELECT * FROM "Order"': 'sqlite_order', 'SELECT * FROM "Product"': 'sqlite_product',
       'SELECT * FROM "OrderDetail"': 'sqlite_orderdetail', 'db.exec(sql): 8000 statements': 'sqlite_exec8000'}
RTS = ['silkjs', 'bun_nojit', 'bun_nojit_smol', 'bun']


def load(paths, skip_pg_in_first=True):
    samples, extra, errors = {}, {}, {}
    for i, p in enumerate(paths):
        for r in json.load(open(p)):
            b, rt = r['bench'], r['runtime']
            if b == 'postgres' and i == 0 and skip_pg_in_first and len(paths) > 1:
                continue
            if 'error' in r:
                errors.setdefault((b, rt), []).append(r['error'])
                continue
            if b == 'sqlite':
                for k, v in r.items():
                    if k in SQL:
                        samples.setdefault((SQL[k], rt), []).append(v)
                continue
            v = r['value']
            if b == 'postgres':
                v = v * 100000 / r.get('queries', 100000)
            samples.setdefault((b, rt), []).append(v)
            for k in ('rss_mb', 'user_us', 'sys_us'):
                if k in r:
                    extra.setdefault((b, rt, k), []).append(r[k])
    return samples, extra, errors


def main():
    out, paths = sys.argv[1], sys.argv[2:]
    samples, extra, errors = load(paths)
    rows = []
    for order, (bid, label, section, unit, better, before) in enumerate(META):
        for rt in RTS:
            s = samples.get((bid, rt))
            if not s:
                continue
            med = lambda k: round(statistics.median(extra[(bid, rt, k)]), 2) if (bid, rt, k) in extra else ''
            rows.append(dict(order=order, benchmark=bid, label=label, section=section, unit=unit, better=better,
                             runtime=rt, median=round(statistics.median(s), 3), min=round(min(s), 3),
                             max=round(max(s), 3), samples=len(s), user_us=med('user_us'), sys_us=med('sys_us'),
                             rss_mb=med('rss_mb'), silkjs_2026_11_11=before if rt == 'silkjs' else ''))
    if not rows:
        sys.exit('report.py: no samples in ' + ' '.join(paths))
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    by = {(r['benchmark'], r['runtime']): r for r in rows}
    for bid, label, *_ in META:
        cells = ['%s=%s(%d)' % (rt, by[(bid, rt)]['median'], by[(bid, rt)]['samples']) for rt in RTS if (bid, rt) in by]
        print(bid, ' '.join(cells))
    for k, v in errors.items():
        print('ERR', k, v[:1])


if __name__ == '__main__':
    main()
