import {Config} from '@remotion/cli/config';

// Reproducible render defaults. The pipeline overrides scale/quality per stage
// (still, draft, final) from the command line.
Config.setVideoImageFormat('jpeg');
Config.setOverwriteOutput(true);
Config.setChromiumOpenGlRenderer('angle');
