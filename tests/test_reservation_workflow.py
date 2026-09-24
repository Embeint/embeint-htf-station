from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from uuid import uuid4

import pytest

from embeint_htf_station.config import (
    ConfigError, Settings, StageSettings, UicrWriteSettings, parse_stage_settings_from_yaml_text,
)
from embeint_htf_station.stages import StageContext, create_stage
from embeint_htf_station.stages import id_pool
from embeint_htf_station.stages.id_pool import PoolReservation
from embeint_htf_station.stages.infuse_provisioning import InfuseProvisioningStage
from embeint_htf_station.stages.values import resolve_value
from embeint_htf_station.stations.basic import BasicStation, RuntimeConfiguration


class Logger:
    def __init__(self):
        self.messages = []

    async def log(self, level, message):
        self.messages.append(message)


class Publisher:
    def __init__(self):
        self.messages = []

    async def publish(self, topic, payload, qos=0):
        self.messages.append((topic, json.loads(payload)))


@pytest.fixture
def service():
    requests = []
    finish = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            if self.path == '/timeout':
                finish.wait(2)
                return
            self.send_response(302 if self.path == '/redirect' else 201)
            self.send_header('Content-Type', 'application/json')
            if self.path == '/redirect':
                self.send_header('Location', '/consume')
            self.end_headers()
            self.wfile.write(b'{"receipt":{"id":"receipt-1"}}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requests
    finally:
        finish.set()
        server.shutdown()
        server.server_close()
        thread.join()


def station_settings():
    return Settings(org_id='org', station_id='station', station_key='station-secret')


def workflow(url, path='/consume'):
    return parse_stage_settings_from_yaml_text(f'''
stages:
  - name: Reserve
    kind: reserve_variables
    variables: [infuse_id]
  - name: Print ID
    kind: print
    message: 'ID=${{provisioning.infuse_id}}'
    wait_seconds: 0
  - name: External service
    kind: http_request
    http:
      url: {url}{path}
      timeout_seconds: 0.1
      headers:
        Authorization: Bearer external-secret
      json:
        dut: '${{dut_id}}'
        id: '${{provisioning.infuse_id}}'
      outputs:
        external.receipt: receipt.id
  - name: Print receipt
    kind: print
    message: 'Receipt=${{external.receipt}}'
    wait_seconds: 0
  - name: Commit
    kind: commit_variables
    variables: [infuse_id]
''')


@pytest.mark.parametrize('path,expected', [('/consume', 'passed'), ('/timeout', 'failed'), ('/redirect', 'failed')])
async def test_yaml_reserve_use_commit_and_uncertain_http_retains_reservation(monkeypatch, service, path, expected, capsys):
    url, requests = service
    reservation = PoolReservation({'infuse_id': '00001'}, {'infuse_id': str(uuid4())})
    commits = []
    monkeypatch.setattr(id_pool, 'allocate_variables', lambda *args: reservation)

    def commit(settings, dut, ids):
        assert requests and ids == reservation.reservation_ids
        commits.append((dut, ids))
        return reservation

    monkeypatch.setattr(id_pool, 'commit_variables', commit)
    station = BasicStation(station_settings())
    publisher = Publisher()
    stages = workflow(url, path)
    for run in (str(uuid4()), str(uuid4())):
        result = await station._run_test(publisher, 'DUT-1', run, stages=stages, lane='left')
        assert result.outcome == expected
    assert len(requests) == 2  # No automatic retries, including redirects/timeouts.
    assert requests[0][2] == {'dut': 'DUT-1', 'id': '00001'}
    assert requests[0][1]['Idempotency-Key'] == requests[1][1]['Idempotency-Key']
    assert requests[0][1]['Authorization'] == 'Bearer external-secret'
    assert 'X-Station-Key' not in requests[0][1]
    assert len(commits) == (2 if expected == 'passed' else 0)
    assert reservation.values == {'infuse_id': '00001'}
    output = capsys.readouterr().out
    assert 'ID=00001' in output
    assert 'external-secret' not in output and 'station-secret' not in output
    if expected == 'passed':
        assert 'Receipt=receipt-1' in output
    else:
        assert any(p.get('name') == 'Commit' and p.get('status') == 'aborted' for _, p in publisher.messages)
    # A new reservation produces a different service idempotency key.
    reservation.reservation_ids['infuse_id'] = str(uuid4())
    await station._run_test(publisher, 'DUT-1', str(uuid4()), stages=stages, lane='right')
    assert requests[2][1]['Idempotency-Key'] != requests[0][1]['Idempotency-Key']


@pytest.mark.parametrize('failure', ['unknown-stage', 'missing-reference'])
async def test_failed_prerequisite_blocks_service_and_commit(monkeypatch, service, failure):
    url, requests = service
    monkeypatch.setattr(id_pool, 'allocate_variables', lambda *args: PoolReservation({'infuse_id': '1'}, {'infuse_id': str(uuid4())}))
    monkeypatch.setattr(id_pool, 'commit_variables', lambda *args: pytest.fail('Must not commit'))
    stages = list(workflow(url))
    stages.insert(1, StageSettings(name='Prerequisite', kind='missing' if failure == 'unknown-stage' else 'print', message='${missing.value}', wait_seconds=0))
    result = await BasicStation(station_settings())._run_test(Publisher(), 'DUT', str(uuid4()), stages=stages)
    assert result.outcome in {'failed', 'error'}
    assert requests == []


@pytest.mark.parametrize('outcome', [None, 'failed', 'aborted'])
async def test_commit_requires_successful_cross_lane_prerequisite(monkeypatch, outcome):
    monkeypatch.setattr(id_pool, 'commit_variables', lambda *args: pytest.fail('Must not commit'))
    stages = parse_stage_settings_from_yaml_text('''
stages:
  - name: Commit
    kind: commit_variables
    variables: [infuse_id]
    after:
      - lane: other
        stage: Use ID
        outcome: failed
''')
    batch = None
    if outcome is not None:
        future = asyncio.get_running_loop().create_future()
        future.set_result(outcome)
        batch = {('other', 'Use ID'): future}
    result = await BasicStation(station_settings())._run_test(Publisher(), 'DUT', str(uuid4()), stages=stages, batch_results=batch)
    assert result.outcome == 'failed'


@pytest.mark.parametrize('lane,dependency_lane,dependency_stage,batched,expected', [
    ('default', 'default', 'Verify', False, 'passed'),
    ('left', 'left', 'Verify', False, 'passed'),
    ('default', 'default', 'Verify', True, 'passed'),
    ('default', 'default', 'Missing', False, 'failed'),
    ('default', 'other', 'Verify', False, 'failed'),
    ('left', 'default', 'Verify', False, 'failed'),
])
async def test_commit_resolves_completed_same_lane_dependencies(
    monkeypatch, lane, dependency_lane, dependency_stage, batched, expected,
):
    reservation = PoolReservation({'infuse_id': '00001'}, {'infuse_id': str(uuid4())})
    commits = []
    monkeypatch.setattr(id_pool, 'allocate_variables', lambda *args: reservation)

    def commit(settings, dut, ids):
        commits.append((dut, ids))
        return reservation

    monkeypatch.setattr(id_pool, 'commit_variables', commit)
    stages = parse_stage_settings_from_yaml_text(f'''
stages:
  - name: Reserve
    kind: reserve_variables
    variables: [infuse_id]
  - name: Verify
    kind: print
    message: 'Verified ${{provisioning.infuse_id}}'
    wait_seconds: 0
  - name: Commit
    kind: commit_variables
    variables: [infuse_id]
    after:
      - lane: {dependency_lane}
        stage: {dependency_stage}
''')
    batch = {(lane, stage.name): asyncio.get_running_loop().create_future() for stage in stages} if batched else None
    result = await BasicStation(station_settings())._run_test(
        Publisher(), 'DUT-1', str(uuid4()), stages=stages, lane=lane, batch_results=batch,
    )
    assert [(stage.name, stage.outcome) for stage in result.stages] == [
        ('Reserve', 'passed'), ('Verify', 'passed'), ('Commit', expected),
    ]
    assert result.outcome == expected
    assert commits == ([('DUT-1', reservation.reservation_ids)] if expected == 'passed' else [])


async def test_commit_rejects_changed_or_missing_context_and_failed_prerequisites(monkeypatch):
    monkeypatch.setattr(id_pool, 'commit_variables', lambda *args: pytest.fail('Must not commit'))
    stage = id_pool.CommitVariablesStage(StageSettings(name='Commit', variables=('infuse_id',)), station_settings())
    context = StageContext('DUT')
    assert (await stage.run(Logger(), context)).outcome == 'failed'
    id_pool.store_reservation(context, 'Reserve', PoolReservation({'infuse_id': '00001'}, {'infuse_id': str(uuid4())}))
    context.set_output('Other stage', 'provisioning.infuse_id', '999')
    assert (await stage.run(Logger(), context)).outcome == 'failed'
    context.set_output('Restore', 'provisioning.infuse_id', '00001')
    context.prerequisites_passed = False
    assert (await stage.run(Logger(), context)).outcome == 'failed'


def test_commit_client_retries_exact_tokens_after_uncertain_response(monkeypatch):
    from io import BytesIO
    tokens = {'infuse_id': str(uuid4())}
    calls = []

    def response(request, timeout):
        calls.append(json.loads(request.data))
        assert request.full_url.endswith('/variables/commit')
        if len(calls) == 1:
            raise TimeoutError()
        return BytesIO(json.dumps({'dutId': 'DUT', 'values': {'infuse_id': '00001'}, 'reservationIds': tokens}).encode())

    monkeypatch.setattr(id_pool, 'urlopen', response)
    with pytest.raises(id_pool.IdPoolError, match='uncertain'):
        id_pool.commit_variables(station_settings(), 'DUT', tokens)
    assert id_pool.commit_variables(station_settings(), 'DUT', tokens).values == {'infuse_id': '00001'}
    assert calls == [{'dutId': 'DUT', 'reservationIds': tokens}] * 2


@pytest.mark.parametrize('source', ['infuse_api', 'id_pool'])
async def test_context_only_programming_makes_no_allocation(monkeypatch, tmp_path, source):
    monkeypatch.setattr('embeint_htf_station.stages.infuse_provisioning.allocate_variables', lambda *args: pytest.fail('Must not allocate'))
    monkeypatch.setattr('embeint_htf_station.stages.infuse_provisioning.InfuseProvisioningStage._resolve_infuse_values', lambda *args: pytest.fail('Must not call Infuse'))
    commands = []

    async def command(args, logger):
        commands.append(args)

    settings = StageSettings(name='Program', provisioning_source=source, constants=('infuse_id',), uicr=(
        UicrWriteSettings(name='infuse_id', source='context', value='provisioning.infuse_id', address=0x1000, width_bits=64),
    ))
    context = StageContext('DUT')
    context.set_output('Reserve', 'provisioning.infuse_id', '00009')
    stage = InfuseProvisioningStage(settings, {}, station_settings().model_copy(update={'firmware_cache_dir': str(tmp_path)}), command)
    assert (await stage.run(Logger(), context)).outcome == 'passed'
    assert len(commands) == 1
    from pathlib import Path
    assert ':081000000900000000000000' in Path(commands[0][4]).read_text()


async def test_explicit_pool_reference_takes_precedence_over_builtin(monkeypatch, tmp_path):
    def allocate(settings, dut, names, version):
        assert names == ('hardware_id',)
        return PoolReservation({'hardware_id': '00009'}, {'hardware_id': str(uuid4())})
    monkeypatch.setattr('embeint_htf_station.stages.infuse_provisioning.allocate_variables', allocate)
    commands = []

    async def command(args, logger):
        commands.append(args)

    context = StageContext('DUT')
    context.set_output('Read chip', 'hardware_id', '100')
    stage = InfuseProvisioningStage(StageSettings(name='Program', provisioning_source='id_pool', uicr=(
        UicrWriteSettings(name='hardware_id', value='provisioning.hardware_id', address=0x1000, width_bits=32),
    )), {}, station_settings().model_copy(update={'firmware_cache_dir': str(tmp_path)}), command)
    assert (await stage.run(Logger(), context)).outcome == 'passed'
    from pathlib import Path
    assert ':0410000009000000' in Path(commands[0][4]).read_text()


async def test_bad_runtime_configuration_preserves_previous_plan(capsys):
    station = BasicStation(station_settings())
    old_plans = station._plans.copy()
    bad_yaml = 'stages:\n  - name: Program\n    provisioning_source: id-pool\n'
    with pytest.raises(ConfigError, match='provisioning_source'):
        parse_stage_settings_from_yaml_text(bad_yaml)
    station._fetch_runtime_configuration = lambda: RuntimeConfiguration(revision=9, yaml=bad_yaml)
    await station._load_runtime_configuration()
    assert station._plans == old_plans
    assert station._runtime_config_revision is None
    assert 'configuration_invalid' in capsys.readouterr().out


def test_value_references_are_recursive_single_pass_and_require_existing_values():
    context = StageContext('DUT', 'run')
    context.set_output('Reserve', 'provisioning.id', '${not_reexpanded}')
    assert resolve_value({'a': ['${dut_id}', '${run_id}', 'ID=${context.provisioning.id}']}, context) == {
        'a': ['DUT', 'run', 'ID=${not_reexpanded}'],
    }
    with pytest.raises(ValueError, match='unavailable'):
        resolve_value('${missing}', context)


async def test_arbitrary_stage_string_settings_resolve_from_context():
    seen = []
    class Custom:
        def __init__(self, settings):
            seen.append(settings.path)
        async def run(self, logger, context):
            return None
    context = StageContext('DUT')
    context.set_output('Reserve', 'provisioning.id', '00001')
    stage = create_stage(StageSettings(name='Custom', kind='custom', path='/ids/${provisioning.id}'), {'custom': Custom})
    await stage.run(Logger(), context)
    assert seen == ['/ids/00001']
