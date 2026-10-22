#!/usr/bin/env python3
"""Render release notes, optionally update only catalog Changelog fields.

python tools/release_changelogs.py releases/0.11.0-changelogs.json --publish
Published artifact hashes, versions and approval statuses are never changed.
"""
import argparse
import html
import json
import os
from pathlib import Path

from publish_package import api
from render_index import baserow_rows, load_config, select_text


def changelog_text(notes, package):
    baseline = package['previousVersion'] or 'first registry release'
    return '{} {} (previous: {})\n\n{}'.format(
        package['id'], package['version'], baseline,
        '\n'.join('- ' + line for line in package['changes'] + notes['commonChanges']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('notes', type=Path)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    notes = json.loads(args.notes.read_text(encoding='utf-8'))
    root = Path(__file__).resolve().parents[1]
    inventory = json.loads((root / 'releases' / (notes['engineVersion'] + '.json')).read_text())
    expected = {p['id']: p for p in inventory['packages']}
    assert {(p['id'], p['version']) for p in notes['packages']} == {(p['id'], p['version']) for p in expected.values()}
    assert len(notes['packages']) == len(expected)
    for package in notes['packages']:
        assert package['changes'] and all(package['changes'])

    if args.publish:
        cfg = load_config()
        token = os.environ['BASEROW_TOKEN']
        table = cfg['baserow']['tables']['versions']
        rows = baserow_rows(cfg, table, token)
        components = baserow_rows(cfg, cfg['baserow']['tables']['components'], token)
        updates = []
        for package in notes['packages']:
            key = package['id'] + '@' + package['version']
            matches = [r for r in rows if r['Key'] == key]
            assert len(matches) == 1, key
            row = matches[0]
            assert select_text(row['Status']) == 'Approved', key
            assert row['AbiTag'] == inventory['abi'], key
            actual = {(select_text(c['Name']), c['Sha256'], int(c['Bytes'])) for c in components
                      if any(v['value'] == key for v in c['Version'])}
            hashes = {(c['name'], c['sha256'], c['bytes']) for c in expected[package['id']]['components']}
            assert actual == hashes, key
            updates.append((row, changelog_text(notes, package)))
        for row, text in updates:
            path = 'database/rows/table/{}/{}/?user_field_names=true'.format(table, row['id'])
            if row.get('Changelog') != text:
                result = api(cfg, 'PATCH', path, token, {'Changelog': text})
            else:
                result = row
            assert result['Changelog'] == text
            for field in ('Key', 'Version', 'Status', 'AbiTag', 'Components'):
                assert result[field] == row[field], (row['Key'], field)
            print('Verified catalog changelist:', row['Key'])

    e = html.escape
    title = 'PlasmaEngine {} package changelists'.format(notes['engineVersion'])
    lines = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             '<title>' + e(title) + '</title>',
             '<style>body{max-width:900px;margin:40px auto;padding:0 24px;font:16px/1.65 system-ui;background:#10131a;color:#d9e0ec}a{color:#c89af0}h1,h2{color:#fff;line-height:1.25}section{border-top:1px solid #39404e;margin-top:32px;padding-top:20px}li{margin:8px 0}nav{columns:3;column-width:200px}.meta{color:#a7b4c6}code{font-size:.9em}</style></head><body>',
             '<a href="../index.html">Plasma Packages</a><h1>' + e(title) + '</h1>',
             '<p>' + e(notes['comparison']) + '</p><p>All packages in this release:</p><ul>']
    lines += ['<li>' + e(line) + '</li>' for line in notes['commonChanges']]
    lines += ['</ul><nav><ul>']
    lines += ['<li><a href="#{}">{} {}</a></li>'.format(e(p['id']), e(p['id']), e(p['version'])) for p in notes['packages']]
    lines += ['</ul></nav>']
    for p in notes['packages']:
        lines += ['<section id="{}"><h2>{} {}</h2><p class="meta">Previous: {}</p><ul>'.format(
            e(p['id']), e(p['id']), e(p['version']), e(p['previousVersion'] or 'First registry release'))]
        lines += ['<li>' + e(line) + '</li>' for line in p['changes']]
        lines += ['</ul></section>']
    lines += ['</body></html>']
    out = root / 'site' / 'releases' / (notes['engineVersion'] + '.html')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('Rendered', len(notes['packages']), 'package changelists:', out)


if __name__ == '__main__':
    main()
