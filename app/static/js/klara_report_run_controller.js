/*
 * Lifecycle of the ONE report run the screen is currently tracking.
 *
 * Responsibilities: launch/attach to a run, poll it, cancel it, stop when it ends.
 * Non-responsibilities: HTTP details (ReportingApi), what is drawn (klara_report_run_view),
 * and what a status means: the backend's `status` is the only source of truth, this
 * controller never infers or invents a run state. A failed poll is a connectivity issue,
 * never a `failed` run (that is a domain state only the backend can set).
 *
 * Exactly one poller can exist: every (re)attach bumps `generation`, aborts the in-flight
 * request and clears the timer, and every async continuation checks it before touching state.
 * Replacing the polling loop with SSE/WebSocket only touches `schedule()`/`tick()`.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlaraReportRunController = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const STATUS = Object.freeze({
    QUEUED: 'queued', RUNNING: 'running', COMPLETED: 'completed',
    COMPLETED_WITH_ERRORS: 'completed_with_errors', FAILED: 'failed',
    CANCEL_REQUESTED: 'cancel_requested', CANCELLED: 'cancelled',
  });
  const TERMINAL_STATUSES = new Set([
    STATUS.COMPLETED, STATUS.COMPLETED_WITH_ERRORS, STATUS.FAILED, STATUS.CANCELLED,
  ]);
  const isTerminal = status => TERMINAL_STATUSES.has(status);

  // Single place for polling tuning.
  const DEFAULTS = Object.freeze({
    pollIntervalMs: 2500,
    maxConsecutiveTransientErrors: 5,
    maxBackoffMs: 15000,
  });

  // phase: idle | loading | polling | retrying | finished | stopped
  function createReportRunController({api, onChange, options, timers}) {
    const config = Object.assign({}, DEFAULTS, options || {});
    const clock = timers || {setTimeout: (...a) => setTimeout(...a), clearTimeout: id => clearTimeout(id)};
    let generation = 0;
    let timer = null;
    let abort = null;
    let launching = false;
    let state = {runId: null, run: null, phase: 'idle', error: null, cancelling: false, cancelError: null};
    let transientErrors = 0;

    const emit = patch => {
      state = Object.assign({}, state, patch);
      if (onChange) onChange(state);
    };

    function stopPolling() {
      generation += 1;
      if (timer !== null) { clock.clearTimeout(timer); timer = null; }
      if (abort) { abort.abort(); abort = null; }
    }

    function schedule(gen, delay) {
      timer = clock.setTimeout(() => { timer = null; tick(gen); }, delay);
    }

    async function tick(gen) {
      if (gen !== generation) return;
      abort = typeof AbortController === 'function' ? new AbortController() : null;
      let run;
      try {
        run = await api.getRun(state.runId, {signal: abort && abort.signal});
      } catch (error) {
        if (gen !== generation || (error && error.name === 'AbortError')) return;
        return handlePollError(gen, error);
      }
      if (gen !== generation) return;
      transientErrors = 0;
      if (isTerminal(run.status)) {
        emit({run, phase: 'finished', error: null, cancelling: false});
        return;
      }
      // `cancelling` (request sent) hands over to the backend's own cancel_requested status.
      emit({run, phase: 'polling', error: null,
            cancelling: run.status === STATUS.CANCEL_REQUESTED ? false : state.cancelling});
      schedule(gen, config.pollIntervalMs);
    }

    function handlePollError(gen, error) {
      const kind = error && error.kind;
      if (kind === 'transient') {
        transientErrors += 1;
        if (transientErrors <= config.maxConsecutiveTransientErrors) {
          emit({phase: 'retrying', error: {kind, message: error.message}});
          schedule(gen, Math.min(config.maxBackoffMs, config.pollIntervalMs * 2 ** transientErrors));
          return;
        }
        // Give up polling, but the run itself is untouched: it keeps going in the worker.
        emit({phase: 'stopped', error: {kind: 'connection_lost',
          message: 'Se perdió la conexión con el servidor. La ejecución puede seguir en segundo plano.'}});
        return;
      }
      // auth / not_found / client: retrying cannot fix these.
      emit({phase: 'stopped', error: {kind: kind || 'unexpected', message: (error && error.message) || 'Error inesperado.'}});
    }

    // Start (or restart) tracking an existing run: after the 202, after a reload, or for a run
    // created elsewhere. `seed` is shown immediately, before the first answer from the server.
    function track(runId, seed) {
      stopPolling();
      transientErrors = 0;
      const gen = generation;
      emit({runId, run: seed || null, phase: 'loading', error: null, cancelling: false, cancelError: null});
      tick(gen);
    }

    // Creates a run through `createRun()` (-> {run_id, status}) and tracks it.
    // Errors from creation reject to the caller; no run is tracked in that case.
    async function launch(createRun) {
      if (launching) throw new Error('Ya se está creando una ejecución.');
      launching = true;
      try {
        // track() replaces the previous run's poller only once the new run really exists.
        const created = await createRun();
        track(created.run_id, {
          run_id: created.run_id, status: created.status || STATUS.QUEUED, current_stage: null,
          progress: {current: 0, total: 0}, sections: [], planned_sections: [], error: null, result: null, cost: null,
        });
        return created;
      } finally {
        launching = false;
      }
    }

    async function cancel() {
      const {runId, run, cancelling} = state;
      if (!runId || cancelling || (run && (isTerminal(run.status) || run.status === STATUS.CANCEL_REQUESTED))) return;
      emit({cancelling: true, cancelError: null});
      try {
        await api.cancelRun(runId);
      } catch (error) {
        emit({cancelling: false, cancelError: {kind: error.kind, message: error.message}});
        return;
      }
      // Never assume the cancellation took effect: ask the backend right now, then keep polling
      // until it reports the final state.
      stopPolling();
      tick(generation);
    }

    function dispose() {
      stopPolling();
      state = Object.assign({}, state, {phase: 'idle'});
    }

    return {track, launch, cancel, dispose, getState: () => state};
  }

  return {STATUS, TERMINAL_STATUSES, isTerminal, DEFAULTS, createReportRunController};
});
