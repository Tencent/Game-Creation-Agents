/**
 * gate-engine.mjs — Stable public barrel for pipeline gate checks.
 *
 * Gate implementations live in ./gates/*.mjs. gate-registry.mjs continues to
 * import only this module so the public runner surface remains unchanged.
 */

export { canonicalBuildDigest } from './gates/support.mjs';
export {
  gateBasicStructure,
  gateStateJson,
  gateTestsExist,
  gateRubricContract,
  gateScenesImplemented,
  gateTestsPassing,
  gateNoOpenIssues,
  gateProjectedContracts,
  gateCanonicalHelpers,
  gateTestCoverage,
} from './gates/structure.mjs';
export {
  gateAssetManifest,
  gateAssetRuntimeReferences,
  gateAssetsGenerated,
  gateAssetIntegrationReport,
  gateBboxCoverage,
  gateCropCoverage,
  gateAspectDrift,
  gateMattingComplete,
  gateNoFxSpritesheets,
  gateSubjectMargin,
  gateEntityScale,
  gateSlotValidation,
} from './gates/assets.mjs';
export {
  gateVisualVerified,
  gatePlaythroughRecorded,
  gateReleaseWaivers,
  gateVerifyReport,
  gateCriticReport,
  gatePerformance,
  gatePolishRounds,
  gatePlayQuality,
} from './gates/verify.mjs';
export {
  gateGameplayAlignment,
  gatePhysicsAlignment,
  gateVisualAlignment,
  gateElementAlignment,
  gateSceneCoverage,
  gateVisualDiff,
} from './gates/alignment.mjs';
export {
  gateUiProportion,
  gateUiArtParity,
  gateUiBinding,
  gateUiLayout,
  gateStaticTextAsAsset,
  gateUiContract,
} from './gates/ui.mjs';
export {
  materializeLayoutAlignment,
  gateLayoutAlignment,
} from './gates/layout.mjs';
export {
  gateLevelsPresent,
  gateLevelRuntime,
  gateMapLayoutPresent,
  gateNoBakedPath,
  gatePathFromLayout,
  gateHudBinding,
  gateTowerDefenseContract,
  gateInteractionFit,
  gateInteractionContract,
  gateTunnelRisk,
  gatePrefabCoverage,
} from './gates/genre.mjs';
