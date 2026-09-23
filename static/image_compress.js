/* On-phone photo processing: sniff, decode, resize, JPEG-encode, fingerprint.
 * Browser only. All steps are async so the page never freezes. */

import {
  DEFAULT_CONFIG, ACCEPTED_TYPES, fitWithin, formatBytes, shouldKeepOriginal, sniffImage,
} from './image_core.js';

const fail = (code, message) => Object.assign(new Error(message), { code });

export async function sha256Hex(arrayBuffer) {
  const digest = await crypto.subtle.digest('SHA-256', arrayBuffer);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

const ms = (t0) => Math.round(performance.now() - t0);
const typeName = (mime) => (mime || 'unknown').split('/')[1];

async function decode(blob) {
  const url = URL.createObjectURL(blob);
  const img = new Image();
  img.decoding = 'async';
  img.src = url;
  try {
    await img.decode();
    if (!img.naturalWidth || !img.naturalHeight) throw new Error('empty image');
    return { img, url };
  } catch (err) {
    URL.revokeObjectURL(url);
    throw err;
  }
}

const toBlob = (canvas, quality) => new Promise((resolve) => {
  canvas.toBlob((b) => resolve(b), 'image/jpeg', quality);
});

async function original(blob, mime, width, height) {
  const bytes = await blob.arrayBuffer();
  return {
    bytes, mime, width, height, size: bytes.byteLength, compressed: false,
    sha256: await sha256Hex(bytes), original_size: blob.size,
  };
}

export async function processPhoto(blob, cfg, onLog) {
  const c = cfg || DEFAULT_CONFIG;
  const say = (pct, stage, msg) => { try { if (onLog) onLog(pct, stage, msg); } catch { /* logging never breaks processing */ } };
  if (!blob || !blob.size) throw fail('UNSUPPORTED', 'The photo is empty. Take it again.');

  const mime = sniffImage(new Uint8Array(await blob.slice(0, 32).arrayBuffer()));
  if (!mime || !ACCEPTED_TYPES.includes(mime)) {
    throw fail('UNSUPPORTED', 'This file is not a supported photo (JPEG, PNG, WebP or HEIC).');
  }

  let t0 = performance.now();
  let decoded;
  try {
    decoded = await decode(blob);
  } catch {
    if (blob.size <= c.max_upload_bytes) {
      say(null, 'error', `DECODE_FAILED → uploading original ${typeName(mime)} ${formatBytes(blob.size)}`);
      return original(blob, mime, null, null);
    }
    throw fail('TOO_LARGE', `This phone cannot shrink this photo and it is larger than ${formatBytes(c.max_upload_bytes)}.`);
  }

  const { img, url } = decoded;
  const width = img.naturalWidth;
  const height = img.naturalHeight;
  let canvas = null;
  try {
    say(25, 'compress', `read ${width}x${height} ${typeName(mime)} ${formatBytes(blob.size)} (${ms(t0)} ms)`);

    if (shouldKeepOriginal({ mime, width, height, size: blob.size }, c)) {
      say(50, 'compress', 'kept original (already small)');
      say(75, 'compress', 'kept original (already small)');
      return await original(blob, mime, width, height);
    }

    t0 = performance.now();
    const fit = fitWithin(width, height, c.max_dimension);
    canvas = document.createElement('canvas');
    canvas.width = fit.width;
    canvas.height = fit.height;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw fail('ENCODE_FAILED', 'This phone could not process the photo.');
    /* JPEG has no alpha: transparent PNG areas would otherwise turn black. */
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, fit.width, fit.height);
    ctx.drawImage(img, 0, 0, fit.width, fit.height);
    say(50, 'compress', `resized ${fit.width}x${fit.height} (${ms(t0)} ms)`);

    t0 = performance.now();
    const out = await toBlob(canvas, c.jpeg_quality);
    if (!out || !out.size) throw fail('ENCODE_FAILED', 'This phone could not process the photo.');
    say(75, 'compress', `encoded JPEG q=${c.jpeg_quality} ${formatBytes(out.size)} (${ms(t0)} ms)`);
    if (out.size > c.max_upload_bytes) {
      throw fail('TOO_LARGE', `The photo is still larger than ${formatBytes(c.max_upload_bytes)} after shrinking.`);
    }

    const bytes = await out.arrayBuffer();
    return {
      bytes, mime: 'image/jpeg', width: fit.width, height: fit.height, size: bytes.byteLength,
      compressed: true, sha256: await sha256Hex(bytes), original_size: blob.size,
    };
  } finally {
    URL.revokeObjectURL(url);
    img.src = '';
    /* iOS Safari keeps canvas memory until the backing store is shrunk. */
    if (canvas) { canvas.width = 0; canvas.height = 0; }
  }
}
