/**
 * The files the capture page takes (docs/ai/PHASE2.md §1.1, §1.5): what the server reads, told apart by their first
 * bytes as the server does (`images.sniff`), never by name or type: JPEG, PNG, WebP, HEIC/HEIF, AVIF and TIFF photos,
 * and PDFs. A PDF or a multi-page TIFF is a document whose pages all go on one card, as the server and the inbox take
 * it (`images.expand_document`). Its page count is read from the file's structure, without rendering anything, so the
 * tray pairs and joins cards within the server's page limit; null when the file doesn't say. Fork-owned.
 */

/** A format the server reads (`images.sniff`) */
export type ScanKind = "jpeg" | "png" | "webp" | "heif" | "avif" | "tiff" | "pdf";

export interface ScanFile {
  kind: ScanKind;
  /** The card pages it holds: 1 for a photo; a PDF's pages or a TIFF's page frames; null when they can't be counted */
  pages: number | null;
}

/** What the Choose and drop inputs offer: photos and PDFs (the server reads the formats `sniffKind` knows) */
export const SCANNABLE_ACCEPT = "image/*,application/pdf";

/** How much of a file `sniffKind` needs (`images.SNIFF_BYTES`) */
export const SNIFF_BYTES = 16;
/**
 * A PDF larger than this isn't read to count its pages: the server takes files up to 30 MiB (`limits.MAX_FILE_BYTES`)
 * and refuses a larger one whatever it holds
 */
export const PDF_COUNT_MAX_BYTES = 32 * 1024 * 1024;
/** Frames looked at in a TIFF (`images.MAX_TIFF_FRAMES`): more, and it has too many pages for a card */
export const MAX_TIFF_FRAMES = 32;

const HEIF_BRANDS = new Set(["heic", "heix", "hevc", "hevx", "heim", "heis", "hevm", "hevs", "mif1", "msf1"]);
const AVIF_BRANDS = new Set(["avif", "avis"]);
/** `NewSubfileType` bits of a TIFF frame that isn't a page of its own: a reduced-resolution copy (1), a mask (4) */
const TIFF_NOT_A_PAGE = 0b101;
const TIFF_SUBFILE_TYPE = 254;

function ascii(bytes: Uint8Array, start: number, end: number): string {
  return String.fromCharCode(...bytes.subarray(start, end));
}

function startsWith(bytes: Uint8Array, prefix: readonly number[]): boolean {
  return prefix.every((byte, index) => bytes[index] === byte);
}

/** A file's format from its first `SNIFF_BYTES` bytes, as the server decides it (`images.sniff`); null for others */
export function sniffKind(head: Uint8Array): ScanKind | null {
  if (startsWith(head, [0xFF, 0xD8, 0xFF])) {
    return "jpeg";
  }
  if (startsWith(head, [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])) {
    return "png";
  }
  if (ascii(head, 0, 4) === "RIFF" && ascii(head, 8, 12) === "WEBP") {
    return "webp";
  }
  if (startsWith(head, [0x49, 0x49, 0x2A, 0x00]) || startsWith(head, [0x4D, 0x4D, 0x00, 0x2A])) {
    return "tiff";
  }
  if (ascii(head, 4, 8) === "ftyp") {
    const brand = ascii(head, 8, 12);
    if (AVIF_BRANDS.has(brand)) {
      return "avif";
    }
    return HEIF_BRANDS.has(brand) ? "heif" : null;
  }
  return ascii(head, 0, 5) === "%PDF-" ? "pdf" : null;
}

async function readBytes(file: Blob, start: number, length: number): Promise<Uint8Array> {
  return new Uint8Array(await file.slice(start, start + length).arrayBuffer());
}

// ==========================================
// TIFF: the frames of its IFD chain

/**
 * The pages of a TIFF, as the server counts them (`images._tiff_page_frames`): the first frame, and each later one
 * that isn't a reduced-resolution copy or a mask. More than `MAX_TIFF_FRAMES` frames count as one more than that. Only
 * the headers are read. Null for a BigTIFF or a damaged chain.
 */
export async function tiffPageCount(file: Blob): Promise<number | null> {
  const header = await readBytes(file, 0, 8);
  if (header.length < 8) {
    return null;
  }
  const view = new DataView(header.buffer, header.byteOffset, header.byteLength);
  const little = header[0] === 0x49;
  if (view.getUint16(2, little) !== 42) {
    return null; // a BigTIFF (43): Pillow reads it, but it isn't counted here
  }
  let offset = view.getUint32(4, little);
  const seen = new Set<number>();
  let frames = 0;
  let pages = 0;
  while (offset) {
    if (frames === MAX_TIFF_FRAMES) {
      return MAX_TIFF_FRAMES + 1;
    }
    if (seen.has(offset) || offset + 2 > file.size) {
      return null; // a loop or a frame outside the file
    }
    seen.add(offset);
    const countBytes = await readBytes(file, offset, 2);
    const entries = new DataView(countBytes.buffer, countBytes.byteOffset, countBytes.byteLength).getUint16(0, little);
    const ifd = await readBytes(file, offset + 2, entries * 12 + 4);
    if (ifd.length < entries * 12 + 4) {
      return null;
    }
    const fields = new DataView(ifd.buffer, ifd.byteOffset, ifd.byteLength);
    let subfileType = 0;
    for (let entry = 0; entry < entries; entry++) {
      const at = entry * 12;
      if (fields.getUint16(at, little) === TIFF_SUBFILE_TYPE) {
        // LONG by the spec; a SHORT sits in the first two bytes of the value
        subfileType = fields.getUint16(at + 2, little) === 3
          ? fields.getUint16(at + 8, little)
          : fields.getUint32(at + 8, little);
      }
    }
    if (frames === 0 || !(subfileType & TIFF_NOT_A_PAGE)) {
      pages += 1;
    }
    frames += 1;
    offset = fields.getUint32(entries * 12, little);
  }
  return pages || null;
}

// ==========================================
// PDF: the page tree's count

interface PdfObject {
  /** Its dictionary (and, for an object packed in an object stream, all of it) */
  text: string;
  /** Where its stream's data starts, for an object with a stream */
  streamStart: number | null;
  /** Where it was defined: a later definition (an incremental update) replaces an earlier one */
  position: number;
}

/** A dictionary's integer entry, written out or as a reference to an object holding just the number */
function integerEntry(text: string, key: string, objects: ReadonlyMap<number, PdfObject>): number | null {
  const reference = new RegExp(`/${key}\\s+(\\d+)\\s+\\d+\\s+R\\b`).exec(text);
  if (reference) {
    const target = objects.get(Number(reference[1]))?.text.trim() ?? "";
    return /^\d+$/.test(target) ? Number(target) : null;
  }
  const direct = new RegExp(`/${key}\\s+(\\d+)\\b`).exec(text);
  return direct ? Number(direct[1]) : null;
}

async function inflate(data: Uint8Array): Promise<Uint8Array | null> {
  if (typeof DecompressionStream === "undefined" || typeof Response === "undefined") {
    return null;
  }
  try {
    const body = new Response(data as BlobPart).body;
    if (!body) {
      return null;
    }
    const inflated = body.pipeThrough(new DecompressionStream("deflate"));
    return new Uint8Array(await new Response(inflated).arrayBuffer());
  }
  catch {
    return null; // not zlib data, or damaged
  }
}

/** The objects packed in an object stream (PDF 1.5), added to `objects` unless a later definition replaced them */
async function unpackObjectStream(
  bytes: Uint8Array,
  stream: PdfObject,
  objects: Map<number, PdfObject>,
): Promise<void> {
  const count = integerEntry(stream.text, "N", objects);
  const first = integerEntry(stream.text, "First", objects);
  const length = integerEntry(stream.text, "Length", objects);
  if (count === null || first === null || length === null || stream.streamStart === null) {
    return;
  }
  const raw = bytes.subarray(stream.streamStart, stream.streamStart + length);
  const filter = /\/Filter\s*(\[\s*)?\/(\w+)/.exec(stream.text)?.[2];
  if (filter !== undefined && filter !== "FlateDecode") {
    return;
  }
  if (Number(/\/Predictor\s+(\d+)/.exec(stream.text)?.[1] ?? 1) > 1) {
    return; // a predictor isn't undone here (writers use one for xref streams, not object streams)
  }
  const data = filter ? await inflate(raw) : raw;
  if (!data) {
    return;
  }
  const text = new TextDecoder("latin1").decode(data);
  const numbers = text.slice(0, first).trim().split(/\s+/).map(Number);
  for (let index = 0; index < count; index++) {
    const number = numbers[index * 2];
    const offset = numbers[index * 2 + 1];
    if (number === undefined || offset === undefined || !Number.isInteger(number) || !Number.isInteger(offset)) {
      return;
    }
    const next = numbers[index * 2 + 3];
    const end = index + 1 < count && next !== undefined ? first + next : text.length;
    const known = objects.get(number);
    if (!known || known.position < stream.position) {
      objects.set(number, { text: text.slice(first + offset, end), streamStart: null, position: stream.position });
    }
  }
}

/**
 * The pages of a PDF, from its page tree: the catalog's `/Pages` node's `/Count`, the latest definition of each
 * object winning (incremental updates append), objects packed in object streams included. Null when the file doesn't
 * say (no page tree found, an encrypted object stream, a filter other than Flate), or when it's larger than
 * `PDF_COUNT_MAX_BYTES`.
 */
export async function pdfPageCount(file: Blob): Promise<number | null> {
  if (file.size > PDF_COUNT_MAX_BYTES) {
    return null;
  }
  const bytes = new Uint8Array(await file.arrayBuffer());
  // one character per byte, so a character's index is the byte's offset
  const text = new TextDecoder("latin1").decode(bytes);

  const objects = new Map<number, PdfObject>();
  for (const match of text.matchAll(/(\d+)\s+\d+\s+obj\b/g)) {
    const start = (match.index ?? 0) + match[0].length;
    const end = text.indexOf("endobj", start);
    const body = text.slice(start, end < 0 ? undefined : end);
    const keyword = /\bstream(\r\n|\n|\r)/.exec(body);
    objects.set(Number(match[1]), {
      text: keyword ? body.slice(0, keyword.index) : body,
      streamStart: keyword ? start + keyword.index + keyword[0].length : null,
      position: match.index ?? 0,
    });
  }
  const packed = [...objects.values()].filter(object => /\/Type\s*\/ObjStm\b/.test(object.text));
  for (const stream of packed.sort((a, b) => a.position - b.position)) {
    await unpackObjectStream(bytes, stream, objects);
  }

  const latest = (test: (text: string) => boolean) => [...objects.values()]
    .filter(object => test(object.text))
    .sort((a, b) => b.position - a.position)[0];
  const catalog = latest(object => /\/Type\s*\/Catalog\b/.test(object));
  const rootNumber = catalog ? /\/Pages\s+(\d+)\s+\d+\s+R\b/.exec(catalog.text)?.[1] : undefined;
  // without a catalog: the page tree's root is the node without a parent
  const root = rootNumber !== undefined
    ? objects.get(Number(rootNumber))
    : latest(object => /\/Type\s*\/Pages\b/.test(object) && !/\/Parent\b/.test(object));
  const count = root ? integerEntry(root.text, "Count", objects) : null;
  return count !== null && count >= 1 ? count : null;
}

/**
 * What the server would make of the file: its format and the card pages it holds; null for a file it doesn't read
 * (a GIF, a text file, a Word document)
 */
export async function inspectScanFile(file: Blob): Promise<ScanFile | null> {
  const kind = sniffKind(await readBytes(file, 0, SNIFF_BYTES));
  if (!kind) {
    return null;
  }
  if (kind === "pdf") {
    return { kind, pages: await pdfPageCount(file).catch(() => null) };
  }
  if (kind === "tiff") {
    return { kind, pages: await tiffPageCount(file).catch(() => null) };
  }
  return { kind, pages: 1 };
}

/** Whether a file not read yet may hold several pages (a PDF or a TIFF), going by its type and name */
export function mayBeDocument(file: Blob): boolean {
  const type = file.type.toLowerCase();
  if (type === "application/pdf" || type === "image/tiff") {
    return true;
  }
  const name = typeof File !== "undefined" && file instanceof File ? file.name : "";
  return /\.(pdf|tiff?)$/i.test(name);
}
