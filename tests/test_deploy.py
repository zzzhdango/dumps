import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def prepare_deploy(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    script = tmp_path / "deploy.sh"
    shutil.copy2(ROOT / "deploy.sh", script)
    shutil.copy2(ROOT / "state_preflight.py", tmp_path / "state_preflight.py")
    shutil.copy2(ROOT / "state_schema.py", tmp_path / "state_schema.py")
    (tmp_path / "docker-compose.yml").write_text(
        (ROOT / "docker-compose.yml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "BOT_TOKEN=123456:actual-token\n"
        "TELEGRAM_CHAT_ID=-100987654321\n",
        encoding="utf-8",
    )
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    docker = binary_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"docker $*\" >> \"$DOCKER_LOG\"\n"
        "if [[ \"$*\" == \"compose build bot\" && \"${FAIL_BUILD:-0}\" == 1 ]]; then\n"
        "  exit 42\n"
        "fi\n"
        "if [[ \"$*\" == compose\\ run* && \"${FAIL_WRITE:-0}\" == 1 ]]; then\n"
        "  exit 43\n"
        "fi\n"
        "if [[ \"$*\" == \"compose stop bot\" && \"${FAIL_STOP:-0}\" == 1 ]]; then\n"
        "  exit 46\n"
        "fi\n"
        "if [[ \"$1\" == inspect && \"$*\" == *\"{{.Image}}\"* ]]; then\n"
        "  echo sha256:old-image\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"$1\" == inspect && \"$*\" == *Health* ]]; then\n"
        "  [[ \"${NEVER_HEALTHY:-0}\" == 1 ]] && echo starting || echo healthy\n"
        "elif [[ \"$1\" == inspect ]]; then\n"
        "  echo \"${STOP_RESULT_RUNNING:-true}\"\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    chown = binary_dir / "chown"
    chown.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"chown $*\" >> \"$CHOWN_LOG\"\n"
        "[[ \"${FAIL_CHOWN:-0}\" == 1 ]] && exit 44\n"
        "exit 0\n",
        encoding="utf-8",
    )
    chown.chmod(0o755)
    sudo = binary_dir / "sudo"
    sudo.write_text(
        "#!/usr/bin/env bash\n"
        "[[ \"$1\" == -n ]] && shift\n"
        "if [[ \"${FAIL_CHOWN:-0}\" == 1 && \"$1\" == chown ]]; then exit 45; fi\n"
        "exec \"$@\"\n",
        encoding="utf-8",
    )
    sudo.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{binary_dir}:{env['PATH']}"
    env["DOCKER_LOG"] = str(tmp_path / "docker.log")
    env["CHOWN_LOG"] = str(tmp_path / "chown.log")
    return script, env


def test_deploy_migrates_valid_legacy_state_and_normalizes_permissions(
    tmp_path,
):
    script, env = prepare_deploy(tmp_path)
    legacy = tmp_path / "signals_state.json"
    legacy.write_text(json.dumps({"active": {}}), encoding="utf-8")

    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    migrated = tmp_path / "data" / "signals_state.json"
    assert migrated.exists()
    assert not legacy.exists()
    assert migrated.stat().st_mode & 0o777 == 0o600
    assert list((tmp_path / "backups").glob("legacy-state.*.json"))
    chown_log = (tmp_path / "chown.log").read_text(encoding="utf-8")
    assert "chown -R 1000:1000 data" in chown_log
    docker_log = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "compose run --rm --no-deps --entrypoint python bot -c" in docker_log
    assert docker_log.index("compose build bot") < docker_log.index("compose stop bot")


def test_deploy_aborts_and_backs_up_ambiguous_dual_state(tmp_path):
    script, env = prepare_deploy(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "signals_state.json").write_text(
        json.dumps({"active": {"legacy": {}}}),
        encoding="utf-8",
    )
    (tmp_path / "data" / "signals_state.json").write_text(
        json.dumps({"active": {"current": {}}}),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "оба state-файла" in result.stderr
    assert list((tmp_path / "backups").glob("legacy-conflict.*.json"))
    assert list((tmp_path / "backups").glob("current-conflict.*.json"))
    docker_log = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "compose stop bot" not in docker_log
    assert "compose build bot" not in docker_log


def test_deploy_rejects_placeholder_and_enforces_env_mode(tmp_path):
    script, env = prepare_deploy(tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "BOT_TOKEN=123456:replace_me\n"
        "TELEGRAM_CHAT_ID=-100987654321\n",
        encoding="utf-8",
    )
    env_path.chmod(0o644)

    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "placeholder" in result.stderr
    assert env_path.stat().st_mode & 0o777 == 0o600


def test_compose_has_fixed_identity_and_readiness_healthcheck():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "container_name: binance-futures-short-bot" in compose
    assert "healthcheck:" in compose
    assert "/health" in compose


def test_deploy_fails_when_container_never_becomes_healthy(tmp_path):
    script, env = prepare_deploy(tmp_path)
    env["DEPLOY_HEALTH_TIMEOUT"] = "0"
    env["NEVER_HEALTHY"] = "1"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "не достиг состояния healthy" in result.stderr
    assert "rollback" in result.stderr
    docker_log = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "compose stop bot" in docker_log
    assert "image tag sha256:old-image" in docker_log
    assert docker_log.count("compose up -d --no-build") >= 2


def test_semantically_invalid_current_state_fails_before_build_or_stop(tmp_path):
    script, env = prepare_deploy(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    (data / "signals_state.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "provider": "binanceusdm",
                "active": {},
                "pending_events": [{"event_id": "incomplete"}],
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "schema validation" in result.stderr
    docker_log = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "compose build bot" not in docker_log
    assert "compose stop bot" not in docker_log


def test_invalid_json_fails_before_build_or_stop(tmp_path):
    script, env = prepare_deploy(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    (data / "signals_state.json").write_text("{not-json", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    docker_log = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "compose build bot" not in docker_log
    assert "compose stop bot" not in docker_log


def test_failed_chown_build_or_writeability_check_does_not_stop_service(tmp_path):
    for failure in ("FAIL_CHOWN", "FAIL_BUILD", "FAIL_WRITE"):
        case = tmp_path / failure.lower()
        case.mkdir()
        script, env = prepare_deploy(case)
        env[failure] = "1"
        result = subprocess.run(
            ["bash", str(script)],
            cwd=case,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        docker_log = (case / "docker.log").read_text(encoding="utf-8")
        assert "compose stop bot" not in docker_log


def test_failed_stop_while_container_still_running_needs_no_restart(tmp_path):
    script, env = prepare_deploy(tmp_path)
    env["FAIL_STOP"] = "1"
    env["STOP_RESULT_RUNNING"] = "true"

    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "предыдущий контейнер продолжает работу" in result.stderr
    docker_log = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "compose stop bot" in docker_log
    assert "image tag sha256:old-image" not in docker_log
    assert "compose up -d --no-build" not in docker_log


def test_failed_stop_after_container_stopped_triggers_rollback(tmp_path):
    script, env = prepare_deploy(tmp_path)
    env["FAIL_STOP"] = "1"
    env["STOP_RESULT_RUNNING"] = "false"

    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "выполняется rollback" in result.stderr
    docker_log = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "compose stop bot" in docker_log
    assert "image tag sha256:old-image" in docker_log
    assert "compose up -d --no-build --force-recreate" in docker_log
