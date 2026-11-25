"""Producer checks for offline, complete and repeatable directory handoffs."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
from threading import Barrier

import pytest

from medextract_dossier.bundle_export import export_bundle
from medextract_dossier.bundle_format import ENVELOPE_NAME
from medextract_dossier.signing import generate_keypair, verify_signature

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples' / 'marker-mx1'


@pytest.fixture
def keys(tmp_path):
    private, public = tmp_path / 'signing.pem', tmp_path / 'trusted.pem'
    generate_keypair(private, public)
    return private, public


class Report(HTMLParser):
    def __init__(self, data):
        super().__init__()
        self.tags = []
        self.links = []
        self.text = []
        self.feed(data.decode('utf-8'))
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == 'a':
            self.links.append(dict(attrs)['href'])

    def handle_data(self, data):
        self.text.append(data)


def tree(path):
    return {entry.relative_to(path).as_posix(): entry.read_bytes()
            for entry in path.rglob('*') if entry.is_file()}


@pytest.mark.parametrize('omitted', [[], ['cohort-result']])
def test_signed_members_include_only_inputs_and_the_full_review(tmp_path, keys, omitted):
    example = tmp_path / 'example'
    shutil.copytree(EXAMPLE, example)
    (example / 'sources' / 'unlisted.txt').write_bytes(b'unlisted source')
    (example / 'notes').write_bytes(b'operator notes')
    # The producer must not depend on the independent test oracle.
    (example / 'expected.json').unlink()
    book = json.loads((example / 'casebook.json').read_bytes())
    out = tmp_path / 'recipient' / 'handoff'
    assert export_bundle(example, out, keys[0], omitted) == out
    files = tree(out)
    payload, _ = verify_signature(json.loads(files.pop(ENVELOPE_NAME)), keys[1])
    manifest = json.loads(payload)
    expected_members = {'question.json', 'evidence.json', 'decision.json', 'report.html'}
    assert set(files) == expected_members | {s['file'] for s in book['sources']}
    assert manifest == {'schema_version': 1, 'members': [
        {'path': path, 'bytes': len(files[path]), 'sha256': hashlib.sha256(files[path]).hexdigest()}
        for path in sorted(files)]}
    for source in book['sources']:
        assert files[source['file']] == (example / source['file']).read_bytes()
    assert {**json.loads(files['question.json']), **json.loads(files['evidence.json'])} == book
    comparison = json.loads(files['decision.json'])
    digest = hashlib.sha256(json.dumps(comparison, sort_keys=True, ensure_ascii=False,
                                       separators=(',', ':')).encode()).hexdigest()
    expected = json.loads((EXAMPLE / 'expected.json').read_bytes())
    variant = 'without_cohort_result' if omitted else 'baseline'
    assert digest == expected['canonical_output_sha256'][variant]

    report = Report(files['report.html'])
    visible = ' '.join(report.text)
    assert 'Synthetic' in visible
    assert all(p in visible for p in omitted)
    for decision in comparison['decisions']:
        for value in (decision['question_text'], decision['status'], decision['reason'],
                      *decision['eligible_families'], *decision['question_scope'].values()):
            assert str(value) in visible
        for row in decision['ledger']:
            for key in ('passage_id', 'source_id', 'source_file', 'family_id', 'reason', 'quote', 'source_sha256'):
                assert row[key] in visible
            for value in row['scope'].values():
                assert str(value) in visible
            assert f"[{row['start']}, {row['end']})" in visible
            assert row['source_file'] in report.links
    assert all(target in files for target in report.links)


def test_local_report_escapes_questions_and_exact_multibyte_quotes(tmp_path, keys):
    example = tmp_path / 'example'
    shutil.copytree(EXAMPLE, example)
    book = json.loads((example / 'casebook.json').read_bytes())
    question = 'Synthetic comparison of α & β <script>empty</script>?'
    quote = 'A fictional result — <img> & café.\r\nSecond line.'
    book['questions'][0]['text'] = question
    source = next(s for s in book['sources'] if s['id'] == 'cohort')
    passage = next(p for p in book['passages'] if p['source_id'] == source['id'])
    intro = b'Fictional report\r\n'
    raw = intro + quote.encode() + b'\r\n'
    source['sha256'] = hashlib.sha256(raw).hexdigest()
    passage.update(start=len(intro), end=len(raw) - 2, quote=quote)
    (example / source['file']).write_bytes(raw)
    (example / 'casebook.json').write_text(json.dumps(book), encoding='utf-8')
    out = tmp_path / 'handoff'
    export_bundle(example, out, keys[0])
    report = Report((out / 'report.html').read_bytes())
    assert question in report.text and quote in report.text
    assert 'img' not in report.tags and 'script' not in report.tags
    assert (out / source['file']).read_bytes() == raw


@pytest.mark.parametrize('differing', [False, True])
def test_concurrent_writers_publish_one_complete_bundle(tmp_path, keys, differing, monkeypatch):
    out = tmp_path / 'handoff'
    parent_files = set(tmp_path.iterdir())
    barrier = Barrier(2)
    original = Path.rename

    def synchronized_rename(stage, dest):
        if dest == out:
            # Both writers now hold a complete, unpublished directory.
            assert (stage / ENVELOPE_NAME).is_file()
            assert not out.exists()
            barrier.wait(timeout=10)
        return original(stage, dest)

    monkeypatch.setattr(Path, 'rename', synchronized_rename)
    omissions = [[], ['cohort-result'] if differing else []]
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(export_bundle, EXAMPLE, out, keys[0], omit) for omit in omissions]
    winners = []
    for omit, future in zip(omissions, futures):
        if future.exception() is None:
            assert future.result() == out
            winners.append(omit)
        else:
            assert isinstance(future.exception(), FileExistsError)
    assert len(winners) == (1 if differing else 2)
    assert set(tmp_path.iterdir()) == parent_files | {out}
    assert json.loads((out / 'decision.json').read_bytes())['omitted_passages'] == winners[0]
    before = tree(out)
    assert export_bundle(EXAMPLE, out, keys[0], winners[0]) == out
    assert tree(out) == before


@pytest.mark.parametrize('occupied', ['file', 'empty_dir', 'extra_dir', 'extra_file', 'changed', 'symlink'])
def test_preserve_existing_contents_on_conflict(tmp_path, keys, occupied):
    out = tmp_path / 'handoff'
    if occupied == 'file':
        out.write_bytes(b'keep')
    elif occupied == 'empty_dir':
        out.mkdir()
    else:
        export_bundle(EXAMPLE, out, keys[0])
        if occupied == 'extra_dir':
            (out / 'extra').mkdir()
        elif occupied == 'extra_file':
            (out / 'extra').write_bytes(b'keep')
        elif occupied == 'changed':
            (out / 'report.html').write_bytes(b'keep')
        else:
            report = out / 'report.html'
            outside = tmp_path / 'report.html'
            report.rename(outside)
            report.symlink_to(outside)
    before = tree(out) if out.is_dir() else out.read_bytes()
    with pytest.raises(FileExistsError):
        export_bundle(EXAMPLE, out, keys[0])
    assert (tree(out) if out.is_dir() else out.read_bytes()) == before
    if occupied == 'extra_dir':
        assert (out / 'extra').is_dir()
    if occupied == 'symlink':
        assert (out / 'report.html').is_symlink()


def test_key_must_be_outside_example_and_output(tmp_path, keys):
    example = tmp_path / 'example'
    shutil.copytree(EXAMPLE, example)
    out = tmp_path / 'handoff'
    out.mkdir()
    for path in (example / 'extra.pem', out / 'signing.pem'):
        path.symlink_to(keys[0])
        with pytest.raises(ValueError, match='key'):
            export_bundle(example, out, path)
        path.unlink()
    with pytest.raises(ValueError, match='key'):
        export_bundle(example, keys[0], keys[0])


def test_partial_stage_is_cleaned_after_write_failure(tmp_path, keys, monkeypatch):
    out = tmp_path / 'handoff'
    before = set(tmp_path.iterdir())
    original = Path.write_bytes

    def failed_write(path, data):
        if path.name == 'report.html':
            raise OSError('simulated write failure')
        return original(path, data)

    monkeypatch.setattr(Path, 'write_bytes', failed_write)
    with pytest.raises(OSError, match='simulated write failure'):
        export_bundle(EXAMPLE, out, keys[0])
    assert set(tmp_path.iterdir()) == before


def test_reject_source_name_collisions_on_case_insensitive_filesystems(tmp_path, keys):
    example = tmp_path / 'example'
    shutil.copytree(EXAMPLE, example)
    book = json.loads((example / 'casebook.json').read_bytes())
    source = book['sources'][0]
    alias = f"sources/{Path(source['file']).stem.upper()}.txt"
    (example / alias).write_bytes((example / source['file']).read_bytes())
    book['sources'].append({**source, 'id': 'copy', 'file': alias})
    (example / 'casebook.json').write_text(json.dumps(book), encoding='utf-8')
    out = tmp_path / 'handoff'
    with pytest.raises(ValueError):
        export_bundle(example, out, keys[0])
    assert not out.exists()


def test_key_hardlinked_as_a_source_is_rejected(tmp_path, keys):
    example = tmp_path / 'example'
    shutil.copytree(EXAMPLE, example)
    book = json.loads((example / 'casebook.json').read_bytes())
    alias = 'sources/extra.txt'
    (example / alias).hardlink_to(keys[0])
    book['sources'].append({'id': 'extra', 'family_id': book['families'][0]['id'],
                            'file': alias, 'sha256': hashlib.sha256(keys[0].read_bytes()).hexdigest()})
    (example / 'casebook.json').write_text(json.dumps(book), encoding='utf-8')
    out = tmp_path / 'handoff'
    with pytest.raises(ValueError, match='key'):
        export_bundle(example, out, keys[0])
    assert not out.exists()
