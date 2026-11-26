/*
 * One asynchronous KLARA chat question, from the widget's point of view.
 *
 * ask() enqueues the question (POST /chat/runs) and follows the run with the shared run controller
 * (klara_report_run_controller.js) until it ends; the widget only receives callbacks with TEXT to show:
 *
 *   onStage({text, code})          what the agent is doing now (real stage reported by the worker)
 *   onAnswer(result)               the public answer ({answer, conversation_id, dax_query, model_key...})
 *   onError({message, retryable})  the run ended without an answer; retryable = re-sending makes sense
 *   onConnectionLost({message, resume})
 *                                  polling gave up (network); the run keeps going on the server, and
 *                                  resume() checks on the SAME run again: it never re-sends the question
 *   onNotice(text)                 informative message (e.g. resuming a run already in progress)
 *
 * The run id is kept in sessionStorage (per report slug) while the run is open, so a page reload resumes it
 * (resumePending()) and the answer still arrives. Status / stage / error codes come from the backend
 * (app/persistence/chat_run_states.py); their texts live ONLY here, and an unknown code gets a generic text.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlaraChatRun = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const STATUS = Object.freeze({
    QUEUED: 'queued', RUNNING: 'running', COMPLETED: 'completed', FAILED: 'failed', EXPIRED: 'expired',
    CANCEL_REQUESTED: 'cancel_requested', CANCELLED: 'cancelled',
  });
  const TERMINAL_STATUSES = new Set([STATUS.COMPLETED, STATUS.FAILED, STATUS.EXPIRED, STATUS.CANCELLED]);
  const isTerminal = status => TERMINAL_STATUSES.has(status);

  const STAGE_TEXT = Object.freeze({
    queued: 'Tu consulta está en espera...',
    preparing: 'Preparando la consulta...',
    selecting_skills: 'Eligiendo cómo analizar tu pregunta...',
    reading_schema: 'Revisando el modelo de datos...',
    querying_data: 'Consultando los datos...',
    writing_answer: 'Redactando la respuesta...',
  });
  const DEFAULT_STAGE_TEXT = 'Procesando tu consulta...';
  const stageText = code => STAGE_TEXT[code] || DEFAULT_STAGE_TEXT;

  // Ended runs whose question is worth sending again (the failure was not about the question or the report).
  const RESEND_CODES = new Set(['worker_lost', 'expired', 'processing_error', 'chatbot_unavailable']);
  const DEFAULT_ERROR_TEXT = 'Ocurrió un error al procesar la consulta.';
  const LOST_RUN_TEXT = 'No encontré la consulta en curso. Podés volver a enviarla.';
  const RESUME_NOTICE = 'Termino primero la consulta que ya estaba en curso.';
  const STORAGE_PREFIX = 'klara:chat-run:';
  const DEFAULT_POLL_MS = 1000;

  function runErrorView(run) {
    const error = (run && run.error) || {};
    const code = error.code || (run && run.status === STATUS.EXPIRED ? 'expired' : 'processing_error');
    return {message: error.message || DEFAULT_ERROR_TEXT, retryable: RESEND_CODES.has(code), code};
  }

  // sessionStorage may be missing or throw (private mode, blocked site data): the widget must still work.
  function safeStorage(storage) {
    return {
      get(key) { try { return storage ? storage.getItem(key) : null; } catch (_) { return null; } },
      set(key, value) { try { if (storage) storage.setItem(key, value); } catch (_) { /* best effort */ } },
      remove(key) { try { if (storage) storage.removeItem(key); } catch (_) { /* best effort */ } },
    };
  }

  function createChatRunSession({api, createController, storage, slug, callbacks, pollMs}) {
    const on = Object.assign({
      onStage() {}, onAnswer() {}, onError() {}, onConnectionLost() {}, onNotice() {},
    }, callbacks || {});
    const store = safeStorage(storage);
    const key = STORAGE_PREFIX + (slug || '');
    const defaultPollMs = pollMs || DEFAULT_POLL_MS;
    let controller = null;
    let lastStage = null;

    function release() {
      if (controller) controller.dispose();
      controller = null;
    }

    function follow(runId, seed, intervalMs) {
      release();
      lastStage = null;
      store.set(key, runId);
      const current = createController({
        api,
        options: {pollIntervalMs: intervalMs || defaultPollMs, isTerminal},
        onChange: state => handle(current, state, intervalMs),
      });
      controller = current;
      current.track(runId, seed || null);
    }

    function handle(owner, state, intervalMs) {
      if (owner !== controller) return;  // a newer run replaced this one
      const run = state.run;
      if (run && !isTerminal(run.status) && run.stage !== lastStage) {
        lastStage = run.stage;
        on.onStage({text: stageText(run.stage), code: run.stage});
      }
      if (state.phase === 'finished' && run) {
        store.remove(key);
        release();
        if (run.status === STATUS.COMPLETED) on.onAnswer(run.result || {});
        else on.onError(runErrorView(run));
      } else if (state.phase === 'stopped') {
        const error = state.error || {};
        if (error.kind === 'connection_lost') {
          const runId = state.runId;
          release();  // the run id stays stored: a reload can still resume it
          on.onConnectionLost({message: error.message, resume: () => follow(runId, run, intervalMs)});
        } else {
          store.remove(key);
          release();
          on.onError(error.kind === 'not_found' ? {message: LOST_RUN_TEXT, retryable: true}
            : {message: error.message || DEFAULT_ERROR_TEXT, retryable: false});
        }
      }
    }

    // body: {message, slug, conversation_id, model_key}. Resolves to 'queued' | 'resumed' | 'failed'.
    async function ask(body) {
      let created;
      try {
        created = await api.createRun(body);
      } catch (error) {
        if (error && error.kind === 'conflict' && error.code === 'chat_run_in_progress' && error.runId) {
          on.onNotice(RESUME_NOTICE);
          follow(error.runId, null, defaultPollMs);
          return 'resumed';
        }
        on.onError({message: (error && error.message) || DEFAULT_ERROR_TEXT,
                    retryable: Boolean(error && error.kind === 'transient')});
        return 'failed';
      }
      follow(created.run_id, created, created.poll_after_ms);
      return 'queued';
    }

    // After a reload: keep following the run that was open in this tab, if any.
    function resumePending() {
      const runId = store.get(key);
      if (!runId) return false;
      follow(runId, null, defaultPollMs);
      return true;
    }

    return {ask, resumePending, dispose: release, isBusy: () => controller !== null};
  }

  return {
    STATUS, TERMINAL_STATUSES, isTerminal, STAGE_TEXT, DEFAULT_STAGE_TEXT, stageText, runErrorView,
    STORAGE_PREFIX, createChatRunSession,
  };
});
