require('dotenv').config();
const fs = require('fs');
const path = require('path');
const express = require('express');
const cors = require('cors');
const rateLimit = require('express-rate-limit');
const config = require('./config/env');
const store = require('./services/store');
const defaultAI = require('./config/ai');
const { object } = require('./services/validation');

function createApp({ ai = defaultAI, limits = {}, build = path.join(__dirname, '..', 'frontend', 'build') } = {}) {
  const app = express();
  app.locals.ai = ai;
  app.disable('x-powered-by');
  app.set('trust proxy', config.trustProxyHops);
  const origins = new Set(config.clientUrls.map((value) => new URL(value).origin));
  app.use(cors({
    credentials: true,
    origin(origin, callback) {
      if (!origin || origins.has(origin)) return callback(null, true);
      const error = new Error('Origin is not allowed.');
      error.status = 403;
      return callback(error);
    },
  }));
  app.use(express.json({ limit: '256kb' }));
  app.use(express.urlencoded({ extended: false, limit: '256kb' }));
  app.use((req, res, next) => {
    if (req.is('application/json') && !object(req.body)) return res.status(400).json({ error: 'Expected a JSON object.' });
    return next();
  });
  function limiter(windowMs, limit) {
    return rateLimit({ windowMs, limit, standardHeaders: 'draft-7', legacyHeaders: false,
      message: { error: 'Too many requests. Please try again later.' } });
  }
  app.use('/api/auth', limiter(15 * 60 * 1000, limits.auth || 20), require('./routes/auth'));
  app.use('/api/resume', limiter(60 * 1000, limits.resume || 10), require('./routes/resume'));
  const aiLimiter = limiter(60 * 1000, limits.ai || 60);
  app.use('/api/answers', aiLimiter, require('./routes/answers'));
  app.use('/api/sessions', aiLimiter, require('./routes/sessions'));
  app.get(['/health', '/api/status'], (req, res) => res.json({ status: 'ok', version: '1.0.0',
    service: 'Interview Nailer API', node_env: config.nodeEnv, storage_mode: config.storageMode, ai_mode: config.aiMode }));
  if (fs.existsSync(path.join(build, 'index.html'))) {
    app.use(express.static(build));
    app.get('/{*splat}', (req, res, next) => req.path.startsWith('/api/') ? next() : res.sendFile(path.join(build, 'index.html')));
  }
  app.use((req, res) => res.status(404).json({ error: 'Route not found.' }));
  app.use((error, req, res, next) => {
    if (res.headersSent) return next(error);
    const status = error.code === 'LIMIT_FILE_SIZE' ? 413 : error.name === 'MulterError' ? 400 : error.status || 500;
    if (status >= 500 && ![502, 503].includes(status)) console.error('Request failed:', error.name);
    const message = status === 413 ? 'Upload or request is too large.' : status >= 500 && ![502, 503].includes(status)
      ? 'Internal server error.' : status === 400 ? 'Invalid request.' : error.message;
    return res.status(status).json({ error: message });
  });
  return app;
}

async function start() {
  await store.initialize();
  const app = createApp();
  return new Promise((resolve, reject) => {
    const server = app.listen(config.port, () => {
      console.log(`Interview Nailer: http://localhost:${config.port} (${config.storageMode} storage, ${config.aiMode} AI)`);
      resolve(server);
    });
    server.once('error', reject);
  });
}
if (require.main === module) {
  start().catch((error) => { console.error('Failed to start API:', error.message); process.exitCode = 1; });
}
module.exports = { createApp, start };
