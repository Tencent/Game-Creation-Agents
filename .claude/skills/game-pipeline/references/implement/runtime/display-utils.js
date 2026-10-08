import { CONFIG } from './config.js';

function displayFor(entityKey) {
  return CONFIG.sprites?.[entityKey]?.display || null;
}

function finitePositive(value) {
  return Number.isFinite(value) && value > 0;
}

function frameRectToWorld(sprite, entityKey, rect) {
  const display = displayFor(entityKey) || {};
  const scaleX = Math.abs(sprite.scaleX ?? display.scale ?? 1);
  const scaleY = Math.abs(sprite.scaleY ?? display.scale ?? 1);
  const frameWidth = display.frameWidth ?? sprite.width;
  const frameHeight = display.frameHeight ?? sprite.height;
  const left = sprite.x - sprite.originX * frameWidth * scaleX;
  const top = sprite.y - sprite.originY * frameHeight * scaleY;
  const rectX = sprite.flipX
    ? frameWidth - rect.x - rect.w
    : rect.x;
  return {
    x: left + rectX * scaleX,
    y: top + rect.y * scaleY,
    w: rect.w * scaleX,
    h: rect.h * scaleY,
  };
}

export function applyDisplay(sprite, entityKey) {
  sprite.name = entityKey;
  sprite.entityKey = entityKey;
  sprite.setData?.('layoutKey', entityKey);
  sprite.setData?.('entityKey', entityKey);

  const display = displayFor(entityKey);
  if (!display) return sprite;

  if (Array.isArray(display.origin)) {
    sprite.setOrigin(display.origin[0], display.origin[1]);
  } else if (Number.isFinite(display.originY)) {
    sprite.setOrigin(0.5, display.originY);
  }

  const frameBody = (display.bodySpace ?? 'frame') === 'frame';
  const sizedBody = finitePositive(display.bodyWidth)
    && finitePositive(display.bodyHeight);
  if (
    frameBody
    && sizedBody
    && typeof sprite.body?.setSize === 'function'
    && typeof sprite.body?.setOffset === 'function'
  ) {
    const offsetX = Number.isFinite(display.bodyOffsetX)
      ? display.bodyOffsetX
      : 0;
    const offsetY = Number.isFinite(display.bodyOffsetY)
      ? display.bodyOffsetY
      : 0;
    sprite.body.setSize(
      display.bodyWidth,
      display.bodyHeight,
      false,
    );
    sprite.body.setOffset(offsetX, offsetY);
  }

  if (finitePositive(display.scale)) sprite.setScale(display.scale);
  return sprite;
}

export function applyFacing(sprite, entityKey, direction) {
  const display = displayFor(entityKey);
  if (!display || !['left', 'right'].includes(display.facing)) return sprite;

  const facesRight = display.facing === 'right';
  if (direction < 0) sprite.setFlipX(facesRight);
  else if (direction > 0) sprite.setFlipX(!facesRight);

  const sizedBody = finitePositive(display.bodyWidth)
    && finitePositive(display.bodyHeight);
  if (
    sizedBody
    && (display.bodySpace ?? 'frame') === 'frame'
    && Number.isFinite(display.frameWidth)
    && typeof sprite.body?.setOffset === 'function'
  ) {
    const baseX = Number.isFinite(display.bodyOffsetX)
      ? display.bodyOffsetX
      : 0;
    const offsetY = Number.isFinite(display.bodyOffsetY)
      ? display.bodyOffsetY
      : 0;
    const offsetX = sprite.flipX
      ? display.frameWidth - baseX - display.bodyWidth
      : baseX;
    sprite.body.setOffset(offsetX, offsetY);
  }
  return sprite;
}

export function getEffectOrigin(sprite, entityKey, aim) {
  const display = displayFor(entityKey);
  if (!display) return { x: sprite.x, y: sprite.y };

  const forward = Number.isFinite(display.bulletOffsetX)
    ? display.bulletOffsetX
    : 0;
  const lateral = Number.isFinite(display.bulletOffsetY)
    ? display.bulletOffsetY
    : 0;

  if (aim && Number.isFinite(aim.x) && Number.isFinite(aim.y)) {
    const length = Math.hypot(aim.x, aim.y);
    if (length > 0) {
      const vx = aim.x / length;
      const vy = aim.y / length;
      return {
        x: sprite.x + vx * forward - vy * lateral,
        y: sprite.y + vy * forward + vx * lateral,
      };
    }
  }

  if (Number.isFinite(aim) && aim !== 0) {
    return {
      x: sprite.x + Math.sign(aim) * forward,
      y: sprite.y + lateral,
    };
  }

  if (display.facing === 'left' || display.facing === 'right') {
    const nativeRight = display.facing === 'right';
    const direction = sprite.flipX
      ? (nativeRight ? -1 : 1)
      : (nativeRight ? 1 : -1);
    return {
      x: sprite.x + direction * forward,
      y: sprite.y + lateral,
    };
  }
  return { x: sprite.x, y: sprite.y };
}

export function getTextY(sprite, entityKey) {
  const display = displayFor(entityKey);
  return sprite.y + (display?.textOffsetY ?? 0);
}

export function getHitRect(sprite, entityKey) {
  const display = displayFor(entityKey);
  const bbox = display?.contentBbox;
  if (bbox) return frameRectToWorld(sprite, entityKey, bbox);

  const width = sprite.displayWidth;
  const height = sprite.displayHeight;
  return {
    x: sprite.x - width * sprite.originX,
    y: sprite.y - height * sprite.originY,
    w: width,
    h: height,
  };
}

export function getHitCircle(sprite, entityKey) {
  const rect = getHitRect(sprite, entityKey);
  return {
    x: rect.x + rect.w / 2,
    y: rect.y + rect.h / 2,
    r: Math.min(rect.w, rect.h) / 2,
  };
}

export function pointInHitRect(sprite, entityKey, x, y) {
  const rect = getHitRect(sprite, entityKey);
  return (
    x >= rect.x
    && x <= rect.x + rect.w
    && y >= rect.y
    && y <= rect.y + rect.h
  );
}

export function pointInHitCircle(sprite, entityKey, x, y) {
  const circle = getHitCircle(sprite, entityKey);
  const dx = x - circle.x;
  const dy = y - circle.y;
  return dx * dx + dy * dy <= circle.r * circle.r;
}

export function rectsOverlap(a, b) {
  return !(
    a.x + a.w < b.x
    || b.x + b.w < a.x
    || a.y + a.h < b.y
    || b.y + b.h < a.y
  );
}

export function getHurtbox(sprite, entityKey) {
  const display = displayFor(entityKey);
  return display?.hurtbox
    ? frameRectToWorld(sprite, entityKey, display.hurtbox)
    : getHitRect(sprite, entityKey);
}

export function getHitbox(sprite, entityKey, active = true) {
  const display = displayFor(entityKey);
  if (!active || !display?.hitbox) return null;
  return frameRectToWorld(sprite, entityKey, display.hitbox);
}

export function hitboxOverlapsHurtbox(
  attacker,
  attackerKey,
  defender,
  defenderKey,
  active = true,
) {
  const hitbox = getHitbox(attacker, attackerKey, active);
  return hitbox
    ? rectsOverlap(hitbox, getHurtbox(defender, defenderKey))
    : false;
}

function segmentRectEntryT(ax, ay, bx, by, rect) {
  const dx = bx - ax;
  const dy = by - ay;
  let minimum = 0;
  let maximum = 1;
  for (const [point, delta, low, high] of [
    [ax, dx, rect.x, rect.x + rect.w],
    [ay, dy, rect.y, rect.y + rect.h],
  ]) {
    if (Math.abs(delta) < 1e-9) {
      if (point < low || point > high) return null;
      continue;
    }
    let first = (low - point) / delta;
    let second = (high - point) / delta;
    if (first > second) [first, second] = [second, first];
    minimum = Math.max(minimum, first);
    maximum = Math.min(maximum, second);
    if (minimum > maximum) return null;
  }
  return minimum;
}

export function raycast(
  ax,
  ay,
  bx,
  by,
  targets = [],
  blockers = [],
) {
  let best = null;
  let bestT = Infinity;
  const consider = (rect, target) => {
    if (!rect) return;
    const t = segmentRectEntryT(ax, ay, bx, by, rect);
    if (t !== null && t < bestT) {
      bestT = t;
      best = {
        x: ax + (bx - ax) * t,
        y: ay + (by - ay) * t,
        target,
      };
    }
  };
  for (const blocker of blockers) consider(blocker, null);
  for (const target of targets) {
    if (!target || target.active === false) continue;
    const key = target.entityKey
      || target.getData?.('entityKey')
      || target.name
      || target.getData?.('layoutKey');
    consider(getHurtbox(target, key), target);
  }
  return best;
}

export function lineOfSight(ax, ay, bx, by, blockers = []) {
  return blockers.every(
    (rect) => segmentRectEntryT(ax, ay, bx, by, rect) === null,
  );
}

export function applyUiDisplay(
  image,
  {
    profile = 'art_directed',
    targetWidth = null,
    targetHeight = null,
    integerScales = null,
  } = {},
) {
  const source = image.texture?.getSourceImage?.();
  if (!source?.width || !source?.height) return image;
  if (profile === 'art_directed') return image;
  if (!targetWidth && !targetHeight) return image;

  const scaleX = targetWidth ? targetWidth / source.width : Infinity;
  const scaleY = targetHeight ? targetHeight / source.height : Infinity;
  let scale = Math.min(scaleX, scaleY);
  if (!Number.isFinite(scale)) {
    scale = Number.isFinite(scaleX) ? scaleX : scaleY;
  }
  if (profile === 'pixel_art') {
    if (
      !Array.isArray(integerScales)
      || integerScales.length === 0
      || integerScales.some((value) => !Number.isInteger(value) || value < 1)
    ) {
      throw new Error('pixel_art UI requires non-empty positive integerScales');
    }
    const fitting = integerScales
      .filter((value) => value <= scale + 0.0001)
      .sort((a, b) => b - a);
    if (!fitting.length) {
      throw new Error(
        `pixel_art UI cannot shrink below 1x; requested fit scale ${scale.toFixed(3)}`,
      );
    }
    scale = fitting[0];
  }
  if (finitePositive(scale)) image.setScale(scale);
  return image;
}

export function addUiImage(
  scene,
  x,
  y,
  key,
  {
    depth = 1000,
    origin = 0.5,
    parent = null,
    profile = 'art_directed',
    targetWidth = null,
    targetHeight = null,
    integerScales = null,
  } = {},
) {
  const image = scene.add.image(x, y, key).setDepth(depth);
  if (Array.isArray(origin)) image.setOrigin(origin[0], origin[1]);
  else image.setOrigin(origin);
  if (parent) parent.add(image);
  else image.setScrollFactor(0);
  image.name = key;
  image.setData('layoutKey', key);
  return applyUiDisplay(image, {
    profile,
    targetWidth,
    targetHeight,
    integerScales,
  });
}

export function fitUiText(
  text,
  maxWidth,
  maxHeight = Infinity,
  basePx = null,
  minPx = 8,
) {
  let size = basePx ?? (Number.parseInt(text.style.fontSize, 10) || 16);
  text.setFontSize(size);
  while (
    (text.width > maxWidth || text.height > maxHeight)
    && size > minPx
  ) {
    size = Math.max(minPx, Math.floor(size * 0.9));
    text.setFontSize(size);
  }
  return text;
}

export function addUiCluster(
  scene,
  clusterKey,
  anchor,
  keys = [],
  {
    gap = anchor.gap,
    align = 'center',
    depth = 1000,
  } = {},
) {
  if (!Number.isFinite(gap) || gap < 0) {
    throw new Error(`cluster '${clusterKey}' requires an authored non-negative gap`);
  }
  const children = anchor.children
    || (keys.length ? keys : anchor.keys || []).map(
      (key) => ({ kind: 'image', key }),
    );
  const layout = anchor.layout || 'row';
  const container = scene.add
    .container(anchor.x, anchor.y)
    .setDepth(depth);
  container.name = clusterKey;
  container.setData('layoutKey', clusterKey);

  const items = [];
  let cursorX = 0;
  let cursorY = 0;
  let maxWidth = 0;
  let maxHeight = 0;

  for (const child of children) {
    let object;
    if (child.kind !== 'image' && child.kind !== 'text') {
      throw new Error(
        `cluster '${clusterKey}' has unsupported child kind '${child.kind}'`,
      );
    }
    if (child.kind === 'text') {
      const baseFontPx = child.fontPx ?? anchor.fontPx ?? 16;
      const maxChars = Math.max(
        1,
        Math.min(Math.floor(child.maxChars ?? 8), 64),
      );
      object = scene.add.text(0, 0, '8'.repeat(maxChars), {
        fontSize: `${baseFontPx}px`,
        fontFamily: child.fontFamily ?? anchor.fontFamily ?? 'sans-serif',
        color: child.color ?? anchor.color ?? '#ffffff',
        align: child.align || 'left',
      }).setOrigin(0, 0.5);
      const reservedWidth = object.displayWidth;
      const reservedHeight = object.displayHeight;
      const baseSetText = object.setText.bind(object);
      const refit = () => {
        object.setFixedSize(0, 0);
        fitUiText(
          object,
          reservedWidth,
          reservedHeight,
          baseFontPx,
        );
        object.setFixedSize(reservedWidth, reservedHeight);
      };
      object.setText = (value) => {
        baseSetText(value);
        refit();
        return object;
      };
      object.name = child.bind;
      object.setData('layoutKey', child.bind);
      object.setData('bind', child.bind);
      object.setData('maxChars', maxChars);
    } else {
      const key = child.key || child;
      object = scene.add.image(0, 0, key).setOrigin(0, 0.5);
      object.name = key;
      object.setData('layoutKey', key);
      applyUiDisplay(object, {
        profile: child.profile || anchor.profile || 'art_directed',
        targetWidth: child.targetWidth ?? null,
        targetHeight: child.targetHeight ?? null,
        integerScales: child.integerScales ?? anchor.integerScales ?? null,
      });
    }

    if (layout === 'column') {
      object.y = cursorY;
      cursorY += object.displayHeight + gap;
      maxWidth = Math.max(maxWidth, object.displayWidth);
    } else {
      object.x = cursorX;
      cursorX += object.displayWidth + gap;
      maxHeight = Math.max(maxHeight, object.displayHeight);
    }
    container.add(object);
    items.push(object);
  }

  const totalWidth = layout === 'column'
    ? maxWidth
    : Math.max(0, cursorX - gap);
  const totalHeight = layout === 'column'
    ? Math.max(0, cursorY - gap)
    : maxHeight;
  const [originX, originY] = Array.isArray(anchor.origin)
    ? anchor.origin
    : [0, 0.5];

  for (const object of items) {
    if (layout === 'column') {
      object.x -= originX * totalWidth;
      object.y -= originY * totalHeight;
    } else {
      object.x -= originX * totalWidth;
      if (align === 'top') {
        object.y = object.displayHeight / 2 - originY * totalHeight;
      } else if (align === 'bottom') {
        object.y = totalHeight
          - object.displayHeight / 2
          - originY * totalHeight;
      } else {
        object.y = (0.5 - originY) * totalHeight;
      }
    }
  }
  container.setSize(totalWidth, totalHeight);
  container.setScrollFactor(0, 0, true);
  for (const object of items) {
    if (object.getData?.('bind')) object.setText('');
  }
  return { container, items };
}

export function placeUiText(
  scene,
  image,
  anchor = {},
  value = '',
  style = {},
) {
  const width = image.displayWidth;
  const height = image.displayHeight;
  const left = image.x - image.originX * width;
  const top = image.y - image.originY * height;
  const x = left + (anchor.xPct ?? 0.5) * width;
  const y = top + (anchor.yPct ?? 0.5) * height;
  const fontPx = Math.max(
    8,
    Math.round((anchor.fontPct ?? 0.2) * height),
  );
  const align = anchor.align || 'center';
  const originX = align === 'left' ? 0 : align === 'right' ? 1 : 0.5;
  const text = scene.add.text(x, y, String(value), {
    fontFamily: 'sans-serif',
    fontSize: `${fontPx}px`,
    color: '#ffffff',
    ...style,
  }).setOrigin(originX, 0.5).setDepth((image.depth ?? 1000) + 1);

  const parent = image.parentContainer || null;
  if (parent) parent.add(text);
  else text.setScrollFactor(0);

  const maxWidth = (anchor.maxWidthPct ?? 1) * width;
  const maxHeight = (anchor.maxHeightPct ?? 1) * height;
  const baseSetText = text.setText.bind(text);
  text.setText = (next) => {
    baseSetText(next);
    fitUiText(text, maxWidth, maxHeight, fontPx);
    return text;
  };
  fitUiText(text, maxWidth, maxHeight, fontPx);
  const anchorId = typeof anchor.id === 'string' && anchor.id ? anchor.id : null;
  const anchorBindValue = anchor.binds ?? anchor.bind;
  const anchorBind = typeof anchorBindValue === 'string' && anchorBindValue
    ? anchorBindValue
    : null;
  if (anchorId || anchorBind) {
    if (!anchorId || !anchorBind) {
      throw new Error('placeUiText anchors require both id and binds');
    }
    const hostLayoutKey = (
      image.layoutKey
      || image.getData?.('layoutKey')
      || image.name
      || image.texture?.key
    );
    if (typeof hostLayoutKey !== 'string' || !hostLayoutKey) {
      throw new Error('placeUiText requires a file-backed host layout identity');
    }
    text.name = anchorId;
    text.setData('layoutKey', hostLayoutKey);
    text.setData('bind', anchorBind);
  }
  return text;
}

export function bindUiSlots(scene, image, spec, onSelect) {
  const width = image.displayWidth;
  const height = image.displayHeight;
  const left = image.x - image.originX * width;
  const top = image.y - image.originY * height;
  const parent = image.parentContainer || null;
  const zones = [];

  const createZone = (cx, cy, w, h, payload, index, action) => {
    const zone = scene.add
      .zone(left + cx * width, top + cy * height, w * width, h * height)
      .setOrigin(0.5)
      .setDepth((image.depth ?? 1000) + 1)
      .setInteractive({ useHandCursor: true });
    if (parent) parent.add(zone);
    else zone.setScrollFactor(0);
    zone.name = action || String(payload);
    zone.setData('action', action || null);
    zone.on('pointerdown', () => {
      onSelect?.(payload, index, zone, action || null);
    });
    zones.push(zone);
  };

  if (Array.isArray(spec)) {
    spec.forEach((slot, index) => {
      createZone(
        slot.xPct ?? 0.5,
        slot.yPct ?? 0.5,
        slot.wPct ?? 0.2,
        slot.hPct ?? 0.8,
        slot.id ?? slot.action,
        index,
        slot.action,
      );
    });
  } else if (Array.isArray(spec?.items)) {
    const count = spec.items.length;
    if (count === 0) return zones;
    const cellWidth = 1 / count;
    spec.items.forEach((item, index) => {
      createZone(
        (index + 0.5) * cellWidth,
        0.5,
        cellWidth * 0.92,
        0.9,
        item,
        index,
        spec.action,
      );
    });
  }
  return zones;
}

export function tagInteractionLayer(object, layer) {
  if (!object || !layer) return object;
  object.interactionLayer = layer;
  object.setData?.('interactionLayer', layer);
  return object;
}

export function tagEntityInteractionLayer(object, entityKey, contract) {
  if (!object) return object;
  const layer = contract?.entities?.[entityKey]?.layer;
  if (!layer) {
    throw new Error(
      `interaction-contract missing layer for entity '${entityKey}'`,
    );
  }
  if (!contract?.layers?.includes(layer)) {
    throw new Error(
      `interaction-contract entity '${entityKey}' uses undeclared layer '${layer}'`,
    );
  }
  object.entityKey = entityKey;
  object.setData?.('entityKey', entityKey);
  return tagInteractionLayer(object, layer);
}

function tagGroupChildren(group, layer) {
  for (const object of group?.getChildren?.() || []) {
    tagInteractionLayer(object, layer);
  }
}

function interactionLayer(object) {
  return object?.getData?.('interactionLayer')
    || object?.interactionLayer
    || null;
}

function normalizePair(layerA, layerB, objectA, objectB) {
  const actualA = interactionLayer(objectA);
  const actualB = interactionLayer(objectB);
  if (actualA === layerA && actualB === layerB) return [objectA, objectB];
  if (actualA === layerB && actualB === layerA) return [objectB, objectA];
  return [objectA, objectB];
}

function staticLike(object) {
  const body = object?.body;
  return !!body && (body.immovable || body.moves === false);
}

function normalizeOneWayPair(
  actorLayer,
  platformLayer,
  objectA,
  objectB,
) {
  if (staticLike(objectA) && !staticLike(objectB)) return [objectB, objectA];
  if (staticLike(objectB) && !staticLike(objectA)) return [objectA, objectB];
  return normalizePair(actorLayer, platformLayer, objectA, objectB);
}

export function oneWayProcess(actor, platform) {
  const actorBody = actor?.body;
  const platformBody = platform?.body;
  if (!actorBody || !platformBody) return false;
  const now = actor?.scene?.time?.now;
  if (
    Number.isFinite(actorBody.gcaOneWayDropUntil)
    && Number.isFinite(now)
    && now < actorBody.gcaOneWayDropUntil
  ) {
    return false;
  }
  if (!actorBody.prev) return false;
  return (
    actorBody.velocity.y >= 0
    && actorBody.prev.y + actorBody.height <= platformBody.top + 1
  );
}

export function registerInteractions(scene, groups, contract) {
  if (!scene.physics?.add) {
    throw new Error(
      'registerInteractions supports Arcade Physics only; use the authored Matter adapter',
    );
  }
  for (const [layerA, layerB, kind] of contract?.matrix || []) {
    if (kind === 'pick' || kind === 'ignore') continue;
    const groupA = groups[layerA];
    const groupB = groups[layerB];
    if (!groupA || !groupB) {
      throw new Error(
        `interaction layer group missing for [${layerA}, ${layerB}, ${kind}]`,
      );
    }
    tagGroupChildren(groupA, layerA);
    tagGroupChildren(groupB, layerB);
    if (kind === 'collide') {
      scene.physics.add.collider(groupA, groupB, (objectA, objectB) => {
        const [first, second] = normalizePair(
          layerA,
          layerB,
          objectA,
          objectB,
        );
        scene.onInteract?.(layerA, layerB, first, second);
      });
    } else if (kind === 'collide_oneway') {
      scene.physics.add.collider(
        groupA,
        groupB,
        null,
        (objectA, objectB) => {
          const [actor, platform] = normalizeOneWayPair(
            layerA,
            layerB,
            objectA,
            objectB,
          );
          return oneWayProcess(actor, platform);
        },
      );
    } else if (kind === 'overlap') {
      scene.physics.add.overlap(groupA, groupB, (objectA, objectB) => {
        const [first, second] = normalizePair(
          layerA,
          layerB,
          objectA,
          objectB,
        );
        scene.onInteract?.(layerA, layerB, first, second);
      });
    } else {
      throw new Error(`unsupported interaction verb '${kind}'`);
    }
  }
}

export function pickAt(_scene, x, y, group, contract) {
  let best = null;
  let bestDistance = Infinity;
  for (const object of group?.getChildren?.() || []) {
    if (!object.active) continue;
    const key = object.entityKey
      || object.getData?.('entityKey')
      || object.name
      || object.getData?.('layoutKey');
    const shape = contract?.entities?.[key]?.shape;
    if (!['rect', 'circle'].includes(shape)) continue;
    const rect = getHitRect(object, key);
    const centerX = rect.x + rect.w / 2;
    const centerY = rect.y + rect.h / 2;
    const distance = Math.hypot(x - centerX, y - centerY);
    const contains = shape === 'circle'
      ? pointInHitCircle(object, key, x, y)
      : pointInHitRect(object, key, x, y);
    if (contains && distance < bestDistance) {
      best = object;
      bestDistance = distance;
    }
  }
  return best;
}

export function groundProbe(scene, sprite, isStandable, reach = 4) {
  if (typeof isStandable !== 'function') {
    throw new Error('groundProbe requires an authored standable-body predicate');
  }
  const body = sprite.body;
  if (!body || !scene.physics?.overlapRect) return false;
  if (body.blocked?.down === true) return true;
  return scene.physics
    .overlapRect(body.left, body.bottom, body.width, reach, true, true)
    .some((candidate) => (
      candidate !== body
      && candidate?.gameObject !== sprite
      && isStandable(candidate)
    ));
}
