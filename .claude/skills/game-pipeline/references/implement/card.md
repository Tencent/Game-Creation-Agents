# Card Implementation Patterns

> Genre: `card` (Card Games in GameMaker; Card & Board Game in IGDB; no physics, fixed camera). The core loop is draw → play/place → resolve effects → discard or remain on the board. The GDD and `CONFIG` own the deck list, card definitions, resources, targets, effects, turn rules, and chance model; do not add deckbuilding, combat, or meta-progression unless authored.

> For `LEVEL_MODEL=board`, consume the scaffolded deck/board contract. `authored` and `none` consume their declared authority; directory existence never selects routing.

## Deck / Hand / Board State

Keep logical card state separate from Phaser objects. Store card instance IDs in exactly one zone and look up immutable definitions from the authored catalog. When the starting deck lists definition IDs, mint unique instance IDs before play begins.

```javascript
instantiateDeck(definitionIds, rules) {
  if (rules.deckUsesInstanceIds) return [...definitionIds];
  return definitionIds.map((definitionId, index) =>
    `${definitionId}#${index + 1}`
  );
}

createCardState(rules) {
  return {
    turn: 0,
    phase: 'draw',
    energy: rules.startingEnergy,
    deck: this.instantiateDeck(rules.startingDeck, rules),
    hand: [],
    discard: [],
    board: Object.fromEntries(
      rules.boardSlots.map(slot => [slot.id, null])
    ),
    effectQueue: []
  };
}
```

If the game has no board, `boardSlots` is an authored empty array. Phase names are structural; replace them when the GDD defines another turn model.

## Draw + Discard Recycling

Only recycle the discard pile when the authored rules allow it. `shuffleCardIds` must use the authored seeded RNG from `CONFIG.chance` / `cardRules.chanceModel`, and preserve authored order when chance is `none`.

```javascript
drawCards(count) {
  const next = {
    ...this.cardState,
    deck: [...this.cardState.deck],
    hand: [...this.cardState.hand],
    discard: [...this.cardState.discard]
  };

  while (count > 0 && next.hand.length < this.cardRules.maxHandSize) {
    if (next.deck.length === 0) {
      if (!this.cardRules.recycleDiscard || next.discard.length === 0) break;
      next.deck = this.shuffleCardIds(next.discard);
      next.discard = [];
    }
    next.hand.push(next.deck.shift());
    count -= 1;
  }

  this.cardState = next;
  this.refreshCardUi();
}
```

Never create replacement cards when both deck and discard are empty.

## Play + Effect Resolution

Validate phase, cost, target, destination, and every effect type before changing state. `validateCardPlay` returns either `{ ok: false, reason }` or an authored `{ ok: true, plan }`; a rejected play spends nothing and moves no card.

```javascript
playCard(handIndex, targetId = null) {
  const state = this.cardState;
  const instanceId = state.hand[handIndex];
  if (instanceId === undefined) return { ok: false, reason: 'missing-card' };

  const validation = this.validateCardPlay(state, instanceId, targetId);
  if (!validation.ok) return validation;
  const { cost, destination, effects: authoredEffects } = validation.plan;
  const effects = authoredEffects.map(effect => ({
    ...effect, sourceInstanceId: instanceId, targetId
  }));
  for (const effect of effects) this.assertAuthoredEffectType(effect.type);

  const hand = state.hand.filter((_, index) => index !== handIndex);
  const discard = [...state.discard];
  const board = { ...state.board };
  if (destination.type === 'board') board[destination.slotId] = instanceId;
  else discard.push(instanceId);

  this.cardState = {
    ...state,
    energy: state.energy - cost,
    hand, discard, board,
    effectQueue: [...state.effectQueue, ...effects],
    phase: effects.length > 0 ? 'resolve' : state.phase
  };
  this.resolveNextCardEffect();
  this.refreshCardUi();
  return { ok: true };
}

resolveNextCardEffect() {
  const [effect, ...remaining] = this.cardState.effectQueue;
  if (!effect) {
    this.cardState = { ...this.cardState, phase: 'main' };
    this.refreshCardUi();
    return;
  }
  this.cardState = { ...this.cardState, effectQueue: remaining };
  this.applyAuthoredCardEffect(effect, () => {
    this.refreshCardUi();
    this.resolveNextCardEffect();
  });
}
```

Disable additional play input during committal resolution unless the GDD authors responses or an interrupt stack. Animation may delay the next dequeue, but it is not gameplay state.

## Hand / Board UI

- Render cards, panels, counters, and slots from the declared source strategies in the manifest and layout contract; do not rebuild final UI with hardcoded rectangles or static `add.text` labels when the profile requires file-backed or runtime-text widgets.
- Use `placeUiText` for dynamic resource/deck/discard values and `bindUiSlots` for authored hand/board hit regions.
- Slot callbacks pass instance/slot IDs into the state methods; Phaser zones never own deck state.
- Reconcile or destroy old card views when state changes so handlers do not accumulate.

After every transition, each instance ID must occur exactly once across deck, hand, discard, and occupied board slots.
