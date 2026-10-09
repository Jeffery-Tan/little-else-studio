"""Explicit, local Codex browser login. Never reads credentials or calls a model."""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .agents.codex_client import CodexClient, CodexUnavailable, resolve_codex_command
from .models import PipelineError


def _environment():
    return {key: value for key, value in os.environ.items()
            if not ((key.upper().startswith('CODEX_')
                     and key.upper() not in {'CODEX_HOME', 'CODEX_CA_CERTIFICATE'})
                    or key.upper() in {'OPENAI_API_KEY', 'OPENAI_BASE_URL',
                                       'OPENAI_ORG_ID', 'OPENAI_PROJECT_ID'})}


class CodexAuth:
    def __init__(self, jobs, *, resolver=None, runner=None, spawn=None, timeout=600, model_catalog=None):
        self.jobs = jobs
        self.resolve = resolver or resolve_codex_command
        self.run = runner or subprocess.run
        self.spawn = spawn or subprocess.Popen
        self.timeout = timeout
        self.model_catalog = model_catalog or self._model_catalog
        self.process = None
        self.attempt_id = None
        self.result = None
        self.checked_at = 0.0
        self.closed = False

    def _model_catalog(self):
        return CodexClient(run_id='model-catalog', role='orchestrator', data_root='.',
                           command=self.resolve(), timeout=45).list_models()

    def models(self):
        try:
            return self.model_catalog()
        except (CodexUnavailable, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(503, '暂时无法读取本机 Codex 模型列表；原有模型设置未改变。') from exc

    def _state(self, status, message, auth_method=None):
        return {'status': status, 'message': message, 'auth_method': auth_method,
                'login_active': self.process is not None, 'attempt_id': self.attempt_id}

    def _probe(self):
        try:
            command = self.resolve()
            with tempfile.TemporaryDirectory(prefix='studio-codex-status-') as cwd:
                result = self.run([command, 'login', 'status'], cwd=cwd, env=_environment(),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                                  errors='replace', timeout=8,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            # CLI diagnostics can contain account/key metadata. Return only our fixed messages.
            lines = [line.strip().lower() for line in (result.stdout or '').splitlines()]
            if result.returncode == 0 and 'logged in using chatgpt' in lines:
                return self._state('logged_in', '已通过 ChatGPT 登录，可以使用 Codex 连接。', 'chatgpt')
            if result.returncode == 0 and any(line.startswith(('logged in using an api key',
                                                               'logged in using api key')) for line in lines):
                return self._state('failed', '当前 CLI 使用 API Key 登录。工作台需要 ChatGPT 登录；请在终端自行确认账户切换后刷新。', 'api_key')
            if result.returncode != 0 and 'not logged in' in lines:
                return self._state('not_logged_in', '尚未登录。点击授权登录，在浏览器完成 ChatGPT 账号授权。')
            return self._state('failed', '暂时无法确认 CLI 登录方式，请在终端运行 codex login status 检查后刷新。', 'unknown')
        except CodexUnavailable:
            return self._state('unavailable', '未找到 Codex CLI，请先安装 Codex CLI 或桌面客户端后刷新。')
        except (OSError, subprocess.SubprocessError):
            return self._state('failed', '无法检查 Codex 登录状态，请稍后刷新或在终端检查 CLI。')

    def status(self):
        with self.jobs.lock:
            if self.process is not None:
                return self._state('pending', '已发起登录，请在系统浏览器完成 ChatGPT 授权；完成后这里会自动更新。')
            if self.result is None or time.monotonic() - self.checked_at >= 2:
                current = self._probe()
                if not (self.result and self.result['status'] == 'failed'
                        and current['status'] == 'not_logged_in'):
                    self.result = current
                self.checked_at = time.monotonic()
            return dict(self.result)

    def require_idle(self):
        # Called inside the same JobManager lock used by start(), before any model preparation.
        if self.process is not None:
            raise PipelineError('codex_login_pending', '正在授权 Codex 登录，请完成或取消登录后再开始制作。')

    def start(self):
        with self.jobs.lock:
            if self.closed or self.jobs.closing:
                raise HTTPException(409, '服务正在停止，请重新连接工作台。')
            if self.process is not None:
                return self.status()
            if self.jobs.has_executions():
                raise HTTPException(409, '当前制作仍在运行，请结束后再授权登录。')
            self.result = self._probe()
            self.checked_at = time.monotonic()
            if self.result['status'] != 'not_logged_in':
                return dict(self.result)
            directory = tempfile.TemporaryDirectory(prefix='studio-codex-login-')
            try:
                process = self.spawn(
                    [self.resolve(), 'login', '-c', 'forced_login_method="chatgpt"'],
                    cwd=directory.name, env=_environment(), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except (OSError, CodexUnavailable):
                directory.cleanup()
                self.result = self._state('failed', '未能打开授权登录，请检查 Codex CLI 后重试。')
                return dict(self.result)
            self.process = process
            self.attempt_id = str(uuid.uuid4())
            threading.Thread(target=self._watch, args=(process, directory), daemon=True).start()
            return self.status()

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def _watch(self, process, directory):
        try:
            try:
                code = process.wait(timeout=self.timeout)
                message = '授权未完成，请重新点击授权登录。'
            except subprocess.TimeoutExpired:
                self._stop(process)
                code, message = -1, '授权等待已超时，请重新点击授权登录。'
            with self.jobs.lock:
                if self.process is not process:
                    return
                self.process = None
                self.result = self._probe() if code == 0 else self._state('failed', message)
                if self.result['status'] == 'not_logged_in':
                    self.result = self._state('failed', '授权尚未保存成功，请重试或在终端检查登录。')
                self.checked_at = time.monotonic()
        except (OSError, subprocess.SubprocessError):
            with self.jobs.lock:
                if self.process is process:
                    # Keep the guard if the OS has not confirmed termination.
                    if process.poll() is not None:
                        self.process = None
                    self.result = self._state('failed', '登录进程未正常结束，请取消后重试。')
        finally:
            directory.cleanup()

    def cancel(self, attempt_id):
        with self.jobs.lock:
            if attempt_id != self.attempt_id:
                raise HTTPException(409, '登录状态已经变化，请刷新后再操作。')
            if self.process is not None:
                try:
                    self._stop(self.process)
                except (OSError, subprocess.SubprocessError) as exc:
                    raise HTTPException(409, '无法停止登录进程，请稍后重试。') from exc
                self.process = None
                self.result = self._probe()
                self.checked_at = time.monotonic()
            return self.status()

    def close(self):
        with self.jobs.lock:
            self.closed = True
            if self.process is not None:
                self.cancel(self.attempt_id)


class LoginIn(BaseModel):
    consent: bool = Field(strict=True)


class CancelLoginIn(BaseModel):
    attempt_id: str = Field(min_length=1, max_length=64)


def install_codex_auth_routes(app, auth):
    from fastapi.responses import JSONResponse

    def response(value):
        return JSONResponse(value, headers={'Cache-Control': 'no-store'})

    @app.get('/api/connections/codex/status')
    def status():
        return response(auth.status())

    @app.get('/api/connections/codex/models')
    def models():
        return response(auth.models())

    @app.post('/api/connections/codex/login')
    def login(payload: LoginIn):
        if not payload.consent:
            raise HTTPException(400, '请明确点击授权登录。')
        return response(auth.start())

    @app.post('/api/connections/codex/login/cancel')
    def cancel(payload: CancelLoginIn):
        return response(auth.cancel(payload.attempt_id))
