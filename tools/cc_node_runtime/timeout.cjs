// Load explicitly with node --require before the unmodified CC CLI.
'use strict';

const {getGlobalDispatcher, setGlobalDispatcher} = require('undici');
const timeout = Number(process.env.API_TIMEOUT_MS);
if (!Number.isSafeInteger(timeout) || timeout <= 0 || timeout > 2147483647) {
  throw new Error('API_TIMEOUT_MS must be a positive integer within the Node timer range');
}
const base = new URL(process.env.ANTHROPIC_BASE_URL);
if (!['http:', 'https:'].includes(base.protocol)) {
  throw new Error('ANTHROPIC_BASE_URL must use HTTP or HTTPS');
}
const origin = base.origin;

// Preserve the existing dispatcher and connection settings for every origin.
// AbortSignals still enforce SDK timeouts and user cancellation independently.
setGlobalDispatcher(getGlobalDispatcher().compose(dispatch => (options, handler) =>
  dispatch(String(options.origin) === origin
    ? {...options, headersTimeout: timeout, bodyTimeout: timeout} : options, handler)));
