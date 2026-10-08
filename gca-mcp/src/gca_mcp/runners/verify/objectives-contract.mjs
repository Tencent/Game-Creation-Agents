const OBJECTIVE_KEYS = new Set([
  'id',
  'operation',
  'expected_result',
  'max_steps',
  'scenes',
  'setup',
  'success',
]);
const SETUP_KEYS = new Set([
  'scene',
  'stop',
  'data',
  'state',
  'snapshot',
  'eval',
  'js',
]);
const SUCCESS_OPERATORS = new Set(['>', '>=', '<', '<=', '==', '!=']);

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function nonEmptyString(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

function rejectUnknown(value, allowed, at, errors) {
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) errors.push(`${at}.${key}: unknown field`);
  }
}

function requireFields(value, fields, at, errors) {
  for (const field of fields) {
    if (!Object.prototype.hasOwnProperty.call(value, field)) {
      errors.push(`${at}.${field}: required`);
    }
  }
}

function validateStringArray(value, at, errors) {
  if (!Array.isArray(value)) {
    errors.push(`${at}: expected array`);
    return;
  }
  const seen = new Set();
  value.forEach((entry, index) => {
    if (!nonEmptyString(entry)) errors.push(`${at}[${index}]: expected non-empty string`);
    if (seen.has(entry)) errors.push(`${at}[${index}]: duplicate value`);
    seen.add(entry);
  });
}

function validateSetup(setup, at, errors) {
  if (!isObject(setup)) {
    errors.push(`${at}: expected object`);
    return;
  }
  if (Object.keys(setup).length === 0) errors.push(`${at}: needs at least one field`);
  rejectUnknown(setup, SETUP_KEYS, at, errors);

  for (const field of ['scene', 'eval', 'js']) {
    if (Object.prototype.hasOwnProperty.call(setup, field) && !nonEmptyString(setup[field])) {
      errors.push(`${at}.${field}: expected non-empty string`);
    }
  }
  if (Object.prototype.hasOwnProperty.call(setup, 'stop')) {
    validateStringArray(setup.stop, `${at}.stop`, errors);
  }
  if (
    (Object.prototype.hasOwnProperty.call(setup, 'stop')
      || Object.prototype.hasOwnProperty.call(setup, 'data'))
    && !Object.prototype.hasOwnProperty.call(setup, 'scene')
  ) {
    errors.push(`${at}.scene: required when stop or data is present`);
  }
  if (Object.prototype.hasOwnProperty.call(setup, 'data') && !isObject(setup.data)) {
    errors.push(`${at}.data: expected object`);
  }
  if (Object.prototype.hasOwnProperty.call(setup, 'state')) {
    if (!isObject(setup.state)) {
      errors.push(`${at}.state: expected object`);
    } else {
      for (const [scene, state] of Object.entries(setup.state)) {
        if (!nonEmptyString(scene)) errors.push(`${at}.state: scene key must be non-empty`);
        if (!isObject(state)) errors.push(`${at}.state.${scene}: expected object`);
      }
    }
  }
  if (Object.prototype.hasOwnProperty.call(setup, 'snapshot')) {
    const snapshot = setup.snapshot;
    if (!isObject(snapshot) && !nonEmptyString(snapshot)) {
      errors.push(`${at}.snapshot: expected object or non-empty string`);
    }
  }
}

function validateSuccess(success, at, errors) {
  if (!isObject(success)) {
    errors.push(`${at}: expected object`);
    return;
  }

  const hasActiveScene = Object.prototype.hasOwnProperty.call(success, 'activeScene');
  const hasCustom = Object.prototype.hasOwnProperty.call(success, 'custom');
  const hasGroup = Object.prototype.hasOwnProperty.call(success, 'group');
  const modeCount = Number(hasActiveScene) + Number(hasCustom) + Number(hasGroup);
  if (modeCount !== 1) {
    errors.push(`${at}: choose exactly one predicate form: activeScene, custom, or group`);
    return;
  }

  let allowed;
  let required;
  if (hasActiveScene) {
    allowed = new Set(['activeScene']);
    required = ['activeScene'];
  } else if (hasCustom) {
    allowed = new Set(['scene', 'custom', 'op', 'value']);
    required = ['scene', 'custom', 'op', 'value'];
  } else {
    allowed = new Set(['scene', 'group', 'field', 'op', 'value']);
    required = ['scene', 'group', 'field', 'op', 'value'];
  }
  rejectUnknown(success, allowed, at, errors);
  requireFields(success, required, at, errors);

  for (const field of required.filter((field) => !['op', 'value'].includes(field))) {
    if (!nonEmptyString(success[field])) errors.push(`${at}.${field}: expected non-empty string`);
  }
  if (Object.prototype.hasOwnProperty.call(success, 'op') && !SUCCESS_OPERATORS.has(success.op)) {
    errors.push(`${at}.op: unsupported comparator ${JSON.stringify(success.op)}`);
  }
  if (Object.prototype.hasOwnProperty.call(success, 'value')) {
    const value = success.value;
    if (value !== null && !['number', 'string', 'boolean'].includes(typeof value)) {
      errors.push(`${at}.value: expected number, string, boolean, or null`);
    }
  }
}

export function validateObjectivesDocument(document) {
  const errors = [];
  if (!isObject(document)) return ['$: expected object'];
  rejectUnknown(document, new Set(['schema_version', 'objectives']), '$', errors);
  if (document.schema_version !== 1) errors.push('$.schema_version: must equal 1');
  if (!Array.isArray(document.objectives) || document.objectives.length === 0) {
    errors.push('$.objectives: expected a non-empty array');
    return errors;
  }

  const ids = new Set();
  document.objectives.forEach((objective, index) => {
    const at = `$.objectives[${index}]`;
    if (!isObject(objective)) {
      errors.push(`${at}: expected object`);
      return;
    }
    rejectUnknown(objective, OBJECTIVE_KEYS, at, errors);
    requireFields(objective, ['id', 'operation', 'expected_result'], at, errors);
    for (const field of ['id', 'operation', 'expected_result']) {
      if (!nonEmptyString(objective[field])) errors.push(`${at}.${field}: expected non-empty string`);
    }
    if (nonEmptyString(objective.id)) {
      if (!/^[a-z0-9][a-z0-9_-]*$/.test(objective.id)) {
        errors.push(`${at}.id: expected a filesystem-safe objective id`);
      }
      if (ids.has(objective.id)) errors.push(`${at}.id: duplicate objective id`);
      ids.add(objective.id);
    }
    if (
      Object.prototype.hasOwnProperty.call(objective, 'max_steps')
      && (!Number.isInteger(objective.max_steps) || objective.max_steps < 1)
    ) {
      errors.push(`${at}.max_steps: expected a positive integer`);
    }
    if (Object.prototype.hasOwnProperty.call(objective, 'scenes')) {
      validateStringArray(objective.scenes, `${at}.scenes`, errors);
    }
    if (Object.prototype.hasOwnProperty.call(objective, 'setup')) {
      validateSetup(objective.setup, `${at}.setup`, errors);
    }
    if (Object.prototype.hasOwnProperty.call(objective, 'success')) {
      validateSuccess(objective.success, `${at}.success`, errors);
    }
  });
  if (!ids.has('start_game')) errors.push('$.objectives: must include the start_game objective');
  return errors;
}

export function assertObjectivesDocument(document, source = 'objectives.json') {
  const errors = validateObjectivesDocument(document);
  if (errors.length) {
    throw new Error(`Invalid objectives contract at ${source}:\n- ${errors.join('\n- ')}`);
  }
  return document;
}
