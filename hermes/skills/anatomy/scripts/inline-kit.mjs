import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { ANATOMY_LICENSE } from "../kit/iso-kit.mjs";

const KIT = join(dirname(fileURLToPath(import.meta.url)), "..", "kit");
// Hermes adaptation (not upstream): every inlined script starts with the full upstream MIT
// license as a /*! legal comment, so a page that inlines kit code carries the notice even
// when it is not written by pageHtml().
export const LICENSE_BANNER = `/*! @license Anatomy kit (https://github.com/wheresryan22/anatomy)\n${ANATOMY_LICENSE}*/\n`;
const strip = (file) => LICENSE_BANNER + readFileSync(join(KIT, file), "utf8").replace(/^export /gm, "");

export function kitScript() {
  return strip("iso-kit.mjs");
}

export function glScript() {
  return strip("gl.mjs");
}

export function turnScript() {
  return strip("turn.mjs");
}

export function fixedScript() {
  return strip("turn-fixed.mjs") + "\n" + strip("canvas-painter.mjs").slice(LICENSE_BANNER.length);
}

export function glSharedScript() {
  return strip("gl-shared.mjs");
}
