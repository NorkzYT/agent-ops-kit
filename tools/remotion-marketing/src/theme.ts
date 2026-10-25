import type {Brand} from './schema';

/** Percentage of the frame reserved as a title/action-safe margin. */
export const SAFE_MARGIN_RATIO = 0.06;

export const fonts = {
  display:
    '"SF Pro Display", "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
  body: '"SF Pro Text", "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
};

/** Build a linear brand gradient used as the default background. */
export const brandGradient = (brand: Brand): string =>
  `linear-gradient(135deg, ${brand.primaryColor} 0%, ${brand.secondaryColor} 100%)`;

export const safeMarginPx = (width: number, height: number) => ({
  x: Math.round(width * SAFE_MARGIN_RATIO),
  y: Math.round(height * SAFE_MARGIN_RATIO),
});
