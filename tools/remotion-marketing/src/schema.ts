import {z} from 'zod';
import {zColor} from '@remotion/zod-types';

/**
 * Typed props for the reusable Marketing Video composition.
 *
 * Design notes:
 * - Every on-screen factual claim references a fact by id (`factIds`). The
 *   claims checker cross-checks those against the supplied facts file so the
 *   render cannot assert something the brief never supported.
 * - Every media/music/logo reference is an asset *id*, never a raw path. The
 *   provenance checker resolves ids to a provenance manifest and blocks the
 *   render when commercial-use rights are missing.
 */

export const ASPECT_RATIOS = ['16:9', '9:16', '1:1'] as const;
export type AspectRatio = (typeof ASPECT_RATIOS)[number];

/** Canonical render dimensions per aspect ratio (1080p class). */
export const DIMENSIONS: Record<AspectRatio, {width: number; height: number}> = {
  '16:9': {width: 1920, height: 1080},
  '9:16': {width: 1080, height: 1920},
  '1:1': {width: 1080, height: 1080},
};

export const brandSchema = z.object({
  name: z.string().min(1),
  primaryColor: zColor(),
  secondaryColor: zColor(),
  accentColor: zColor(),
  /** Asset id for the logo, resolved against the provenance manifest. */
  logoAssetId: z.string().optional(),
});

export const productSchema = z.object({
  name: z.string().min(1),
  tagline: z.string().min(1),
});

/** A single supported fact. `evidence` is required and non-empty. */
export const factSchema = z.object({
  id: z.string().min(1),
  claim: z.string().min(1),
  evidence: z.string().min(1),
});

export const SCENE_KINDS = [
  'intro',
  'feature',
  'proof',
  'media',
  'outro',
] as const;

export const sceneSchema = z.object({
  id: z.string().min(1),
  kind: z.enum(SCENE_KINDS),
  durationInFrames: z.number().int().positive(),
  headline: z.string().min(1),
  subhead: z.string().optional(),
  /** Caption shown in the lower safe zone; also used by accessibility QA. */
  caption: z.string().optional(),
  /** Ids of facts this scene displays. Each must exist in the facts file. */
  factIds: z.array(z.string()).default([]),
  /** Optional media asset id (screenshot/clip) for `media` scenes. */
  mediaAssetId: z.string().optional(),
});

export const ctaSchema = z.object({
  text: z.string().min(1),
  url: z.string().optional(),
});

export const MUSIC_MODES = [
  'original',
  'public-domain',
  'licensed',
  'generated',
  'none',
] as const;

export const musicSchema = z.object({
  mode: z.enum(MUSIC_MODES),
  /** Asset id for the audio track; omit only when mode is `none`. */
  assetId: z.string().optional(),
});

export const marketingVideoSchema = z.object({
  fps: z.number().int().positive().default(30),
  aspectRatio: z.enum(ASPECT_RATIOS).default('16:9'),
  brand: brandSchema,
  product: productSchema,
  facts: z.array(factSchema).default([]),
  scenes: z.array(sceneSchema).min(1),
  cta: ctaSchema,
  music: musicSchema,
  captions: z.boolean().default(true),
  safeMargins: z.boolean().default(true),
  /**
   * Maps asset ids to paths under `public/`. The render pipeline builds this
   * from the provenance manifest after rights checks pass, so the composition
   * never hard-codes file locations. Empty for fully generated videos.
   */
  assetPaths: z.record(z.string(), z.string()).default({}),
});

export type MarketingVideoProps = z.infer<typeof marketingVideoSchema>;
export type Scene = z.infer<typeof sceneSchema>;
export type Brand = z.infer<typeof brandSchema>;
export type Fact = z.infer<typeof factSchema>;

/** Total composition length in frames. */
export const totalDurationInFrames = (props: MarketingVideoProps): number =>
  props.scenes.reduce((sum, s) => sum + s.durationInFrames, 0);
