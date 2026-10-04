/**
 * Small files for the recipe card capture tests, made the way their writers make them: photos told apart by their
 * first bytes, PDFs with a classic cross-reference table or with object streams (PDF 1.5), and TIFFs with several
 * frames. Fork-owned.
 */
import { deflateSync } from "node:zlib";

export const JPEG_HEAD = new Uint8Array([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46]);
export const PNG_HEAD = new Uint8Array([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]);

const latin1 = (text: string) => Uint8Array.from(text, char => char.charCodeAt(0));

/** An ISO-BMFF file's head (`ftyp` box) with this brand: `heic`, `avif`, or `isom` for an MP4 */
export function ftypHead(brand: string): Uint8Array<ArrayBuffer> {
  return new Uint8Array([0, 0, 0, 24, ...latin1("ftyp"), ...latin1(brand), 0, 0, 0, 0]);
}

export function file(parts: BlobPart[], name: string, type = ""): File {
  return new File(parts, name, { type });
}

function concat(parts: (string | Uint8Array)[]): Uint8Array<ArrayBuffer> {
  const chunks = parts.map(part => (typeof part === "string" ? latin1(part) : part));
  const out = new Uint8Array(chunks.reduce((total, chunk) => total + chunk.length, 0));
  let at = 0;
  for (const chunk of chunks) {
    out.set(chunk, at);
    at += chunk.length;
  }
  return out;
}

/** `n 0 obj ... endobj` for each body, numbered from `first`, with a classic xref table and trailer */
function classicBody(header: string, bodies: string[], first = 1, previousSize = 0): BlobPart[] {
  const parts: BlobPart[] = [header];
  let offset = header.length;
  const offsets: number[] = [];
  bodies.forEach((body, index) => {
    const object = concat([`${first + index} 0 obj\n`, body, "\nendobj\n"]);
    offsets.push(offset);
    parts.push(object);
    offset += object.length;
  });
  const xref = [
    `xref\n0 1\n0000000000 65535 f \n${first} ${bodies.length}\n`,
    ...offsets.map(at => `${String(at).padStart(10, "0")} 00000 n \n`),
    `trailer\n<< /Size ${Math.max(previousSize, first + bodies.length)} /Root 1 0 R >>\nstartxref\n${offset}\n%%EOF\n`,
  ].join("");
  parts.push(xref);
  return parts;
}

/** A PDF 1.4 with `pages` pages: a catalog, one page tree node and the pages, uncompressed (as Pillow writes) */
export function classicPdf(pages: number, name = "scan.pdf"): File {
  const kids = Array.from({ length: pages }, (_, index) => `${index + 3} 0 R`).join(" ");
  const bodies = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    `<< /Type /Pages /Kids [ ${kids} ] /Count ${pages} >>`,
    ...Array.from({ length: pages }, () => "<< /Type /Page /Parent 2 0 R /MediaBox [ 0 0 612 792 ] >>"),
  ];
  return file(classicBody("%PDF-1.4\n%\xE2\xE3\xCF\xD3\n", bodies), name, "application/pdf");
}

/**
 * A PDF 1.5 whose catalog, page tree and pages are packed in a Flate-compressed object stream, with an xref stream,
 * as most writers save today. `lengthByReference` writes the stream's `/Length` as a reference to another object.
 */
export function objectStreamPdf(pages: number, { lengthByReference = false, name = "scan.pdf" } = {}): File {
  const kids = Array.from({ length: pages }, (_, index) => `${index + 3} 0 R`).join(" ");
  const packed = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    `<< /Type /Pages /Kids [${kids}] /Count ${pages} >>`,
    ...Array.from({ length: pages }, () => "<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>"),
  ];
  let offset = 0;
  const header: string[] = [];
  const bodies: string[] = [];
  packed.forEach((body, index) => {
    header.push(`${index + 1} ${offset}`);
    bodies.push(body);
    offset += body.length + 1;
  });
  const headerText = `${header.join(" ")}\n`;
  const data = deflateSync(latin1(headerText + bodies.join("\n")));
  const stream = pages + 3;
  const length = lengthByReference ? `${stream + 1} 0 R` : String(data.length);
  const xrefData = deflateSync(new Uint8Array(16));
  const objects = concat([
    "%PDF-1.5\n%\xE2\xE3\xCF\xD3\n",
    `${stream} 0 obj\n<< /Type /ObjStm /N ${packed.length} /First ${headerText.length} /Length ${length} /Filter /FlateDecode >>\nstream\r\n`,
    data,
    "\r\nendstream\nendobj\n",
    ...(lengthByReference ? [`${stream + 1} 0 obj\n${data.length}\nendobj\n`] : []),
    `${stream + 2} 0 obj\n<< /Type /XRef /Size ${stream + 3} /W [1 2 1] /Root 1 0 R /Length ${xrefData.length} /Filter /FlateDecode /DecodeParms << /Columns 4 /Predictor 12 >> >>\nstream\n`,
    xrefData,
    "\nendstream\nendobj\nstartxref\n0\n%%EOF\n",
  ]);
  return file([objects], name, "application/pdf");
}

/** A classic PDF of `before` pages, saved again with an incremental update that leaves `after` of them */
export function updatedPdf(before: number, after: number): File {
  const original = classicPdf(before);
  const kids = Array.from({ length: after }, (_, index) => `${index + 3} 0 R`).join(" ");
  const update = [
    `2 0 obj\n<< /Type /Pages /Kids [ ${kids} ] /Count ${after} >>\nendobj\n`,
    "xref\n2 1\n0000000000 00000 n \ntrailer\n<< /Size 9 /Root 1 0 R /Prev 0 >>\nstartxref\n0\n%%EOF\n",
  ].join("");
  return file([original, update], "scan.pdf", "application/pdf");
}

interface TiffFrame {
  /** `NewSubfileType`: 1 a reduced-resolution copy, 4 a mask, 0 a page */
  subfileType?: number;
  /** Write `NewSubfileType` as a SHORT rather than a LONG */
  short?: boolean;
}

/** A TIFF's headers, one IFD per frame (no image data: only the chain is read), little- or big-endian */
export function tiff(frames: TiffFrame[], { bigEndian = false, loop = false, name = "scan.tif" } = {}): File {
  const ifdSize = 2 + 12 + 4;
  const buffer = new ArrayBuffer(8 + frames.length * ifdSize);
  const view = new DataView(buffer);
  const little = !bigEndian;
  view.setUint8(0, bigEndian ? 0x4D : 0x49);
  view.setUint8(1, bigEndian ? 0x4D : 0x49);
  view.setUint16(2, 42, little);
  view.setUint32(4, 8, little);
  frames.forEach((frame, index) => {
    const at = 8 + index * ifdSize;
    view.setUint16(at, 1, little);
    view.setUint16(at + 2, 254, little);
    view.setUint16(at + 4, frame.short ? 3 : 4, little);
    view.setUint32(at + 6, 1, little);
    if (frame.short) {
      view.setUint16(at + 10, frame.subfileType ?? 0, little);
    }
    else {
      view.setUint32(at + 10, frame.subfileType ?? 0, little);
    }
    const last = index === frames.length - 1;
    view.setUint32(at + 14, last ? (loop ? 8 : 0) : at + ifdSize, little);
  });
  return file([buffer], name, "image/tiff");
}
