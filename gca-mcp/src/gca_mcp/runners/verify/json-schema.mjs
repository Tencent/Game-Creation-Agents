export function jsonTypeMatches(value, type) {
  if (type === 'null') return value === null;
  if (type === 'array') return Array.isArray(value);
  if (type === 'object') return value !== null && typeof value === 'object' && !Array.isArray(value);
  if (type === 'integer') return Number.isInteger(value);
  if (type === 'number') return typeof value === 'number' && Number.isFinite(value);
  return typeof value === type;
}

export function validateJsonSchema(value, schema, at = '$', errors = []) {
  if (!schema || typeof schema !== 'object' || errors.length >= 50) return errors;

  if (Object.prototype.hasOwnProperty.call(schema, 'const') && !Object.is(value, schema.const)) {
    errors.push(`${at}: must equal ${JSON.stringify(schema.const)}`);
  }
  for (const child of (schema.allOf || [])) {
    validateJsonSchema(value, child, at, errors);
  }
  if (Array.isArray(schema.anyOf)) {
    const branches = schema.anyOf.map((child) => validateJsonSchema(value, child, at, []));
    if (!branches.some((branchErrors) => branchErrors.length === 0)) {
      const closest = branches.reduce(
        (best, branchErrors) => (branchErrors.length < best.length ? branchErrors : best),
        branches[0] || [],
      );
      errors.push(`${at}: must match at least one allowed shape`);
      errors.push(...closest);
    }
  }
  if (Array.isArray(schema.oneOf)) {
    const branches = schema.oneOf.map((child) => validateJsonSchema(value, child, at, []));
    const matches = branches.filter((branchErrors) => branchErrors.length === 0);
    if (matches.length !== 1) {
      errors.push(`${at}: must match exactly one allowed shape`);
      if (matches.length === 0) {
        const closest = branches.reduce(
          (best, branchErrors) => (branchErrors.length < best.length ? branchErrors : best),
          branches[0] || [],
        );
        errors.push(...closest);
      }
    }
  }

  if (schema.type !== undefined) {
    const types = Array.isArray(schema.type) ? schema.type : [schema.type];
    if (!types.some((type) => jsonTypeMatches(value, type))) {
      errors.push(`${at}: expected ${types.join('|')}`);
      return errors;
    }
  }

  if (Array.isArray(schema.enum) && !schema.enum.some((entry) => Object.is(entry, value))) {
    errors.push(`${at}: must be one of ${schema.enum.map((entry) => JSON.stringify(entry)).join(', ')}`);
  }
  if (typeof value === 'string') {
    if (schema.minLength !== undefined && value.length < schema.minLength) {
      errors.push(`${at}: shorter than minLength ${schema.minLength}`);
    }
    if (schema.pattern) {
      try {
        if (!new RegExp(schema.pattern).test(value)) errors.push(`${at}: does not match ${schema.pattern}`);
      } catch {
        errors.push(`${at}: schema has invalid pattern ${schema.pattern}`);
      }
    }
  }
  if (typeof value === 'number') {
    if (schema.minimum !== undefined && value < schema.minimum) errors.push(`${at}: ${value} < minimum ${schema.minimum}`);
    if (schema.maximum !== undefined && value > schema.maximum) errors.push(`${at}: ${value} > maximum ${schema.maximum}`);
    if (typeof schema.exclusiveMinimum === 'number' && value <= schema.exclusiveMinimum) {
      errors.push(`${at}: ${value} <= exclusiveMinimum ${schema.exclusiveMinimum}`);
    }
  }
  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) errors.push(`${at}: needs at least ${schema.minItems} item(s)`);
    if (schema.maxItems !== undefined && value.length > schema.maxItems) errors.push(`${at}: needs at most ${schema.maxItems} item(s)`);
    if (schema.uniqueItems === true) {
      const seen = new Set();
      for (const entry of value) {
        const key = JSON.stringify(entry);
        if (seen.has(key)) {
          errors.push(`${at}: items must be unique`);
          break;
        }
        seen.add(key);
      }
    }
    if (typeof schema['x-uniqueBy'] === 'string') {
      const field = schema['x-uniqueBy'];
      const seen = new Set();
      for (const [index, entry] of value.entries()) {
        if (
          entry === null
          || typeof entry !== 'object'
          || Array.isArray(entry)
          || !Object.prototype.hasOwnProperty.call(entry, field)
        ) {
          continue;
        }
        const key = JSON.stringify(entry[field]);
        if (seen.has(key)) {
          errors.push(`${at}[${index}].${field}: duplicate value`);
        } else {
          seen.add(key);
        }
      }
    }
    if (schema.contains) {
      const containsMatch = value.some(
        (entry, index) => validateJsonSchema(entry, schema.contains, `${at}[${index}]`, []).length === 0,
      );
      if (!containsMatch) errors.push(`${at}: must contain an item matching the required shape`);
    }
    if (schema.items) value.forEach((entry, index) => validateJsonSchema(entry, schema.items, `${at}[${index}]`, errors));
  }
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    if (schema.minProperties !== undefined && Object.keys(value).length < schema.minProperties) {
      errors.push(`${at}: needs at least ${schema.minProperties} field(s)`);
    }
    if (schema.maxProperties !== undefined && Object.keys(value).length > schema.maxProperties) {
      errors.push(`${at}: needs at most ${schema.maxProperties} field(s)`);
    }
    const properties = schema.properties || {};
    if (schema.propertyNames && typeof schema.propertyNames === 'object') {
      for (const key of Object.keys(value)) {
        validateJsonSchema(key, schema.propertyNames, `${at}.${key} (property name)`, errors);
      }
    }
    for (const key of (schema.required || [])) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) errors.push(`${at}.${key}: required`);
    }
    if (schema.additionalProperties === false) {
      for (const key of Object.keys(value)) {
        if (!Object.prototype.hasOwnProperty.call(properties, key)) errors.push(`${at}.${key}: unknown field`);
      }
    } else if (schema.additionalProperties && typeof schema.additionalProperties === 'object') {
      for (const [key, child] of Object.entries(value)) {
        if (!Object.prototype.hasOwnProperty.call(properties, key)) {
          validateJsonSchema(child, schema.additionalProperties, `${at}.${key}`, errors);
        }
      }
    }
    for (const [key, childSchema] of Object.entries(properties)) {
      if (Object.prototype.hasOwnProperty.call(value, key)) {
        validateJsonSchema(value[key], childSchema, `${at}.${key}`, errors);
      }
    }
  }
  if (schema.if && typeof schema.if === 'object') {
    const conditionErrors = [];
    validateJsonSchema(value, schema.if, at, conditionErrors);
    const branch = conditionErrors.length === 0 ? schema.then : schema.else;
    if (branch && typeof branch === 'object') {
      validateJsonSchema(value, branch, at, errors);
    }
  }
  return errors;
}
