import config from '../../frontend/vite.config.ts';

export default { ...config, cacheDir: process.env.VISUAL_HARNESS_CACHE_DIR };
