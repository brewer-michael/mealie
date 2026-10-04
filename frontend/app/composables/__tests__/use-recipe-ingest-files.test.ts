import { describe, expect, test } from "vitest";
import {
  classicPdf,
  file,
  ftypHead,
  JPEG_HEAD,
  objectStreamPdf,
  PNG_HEAD,
  tiff,
  updatedPdf,
} from "./use-recipe-ingest-files.fixtures";
import {
  inspectScanFile,
  MAX_TIFF_FRAMES,
  mayBeDocument,
  PDF_COUNT_MAX_BYTES,
  pdfPageCount,
  sniffKind,
  tiffPageCount,
} from "~/composables/use-recipe-ingest-files";

const head = (...parts: (Uint8Array | string)[]) =>
  new Uint8Array(parts.flatMap(part => (typeof part === "string" ? Array.from(part, c => c.charCodeAt(0)) : [...part])));

describe("sniffKind: the server's formats, by their first bytes", () => {
  test.each([
    ["jpeg", JPEG_HEAD],
    ["png", PNG_HEAD],
    ["webp", head("RIFF", new Uint8Array(4), "WEBPVP8 ")],
    ["tiff", head("II*", new Uint8Array(1))],
    ["tiff", head("MM", new Uint8Array([0, 42]))],
    ["heif", ftypHead("heic")],
    ["heif", ftypHead("mif1")],
    ["avif", ftypHead("avif")],
    ["pdf", head("%PDF-1.7\n")],
  ])("%s", (kind, bytes) => {
    expect(sniffKind(bytes)).toBe(kind);
  });

  test.each([
    ["a GIF", head("GIF89a")],
    ["a text file", head("Butter, 2 cups")],
    ["an MP4", ftypHead("isom")],
    ["a Word document", head("PK", new Uint8Array([3, 4]))],
    ["an empty file", new Uint8Array()],
  ])("%s is none of them", (_name, bytes) => {
    expect(sniffKind(bytes)).toBeNull();
  });
});

describe("pdfPageCount", () => {
  test.each([1, 2, 4, 7])("a classic PDF with %i pages", async (pages) => {
    expect(await pdfPageCount(classicPdf(pages))).toBe(pages);
  });

  test("a PDF 1.5 whose page tree is packed in a compressed object stream", async () => {
    expect(await pdfPageCount(objectStreamPdf(3))).toBe(3);
  });

  test("an object stream whose length is a reference to another object", async () => {
    expect(await pdfPageCount(objectStreamPdf(2, { lengthByReference: true }))).toBe(2);
  });

  test("an incremental update's page tree replaces the original's", async () => {
    expect(await pdfPageCount(updatedPdf(3, 1))).toBe(1);
  });

  test("a page tree of several levels counts its root", async () => {
    const text = [
      "%PDF-1.4",
      "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj",
      "2 0 obj << /Type /Pages /Kids [3 0 R 4 0 R] /Count 5 >> endobj",
      "3 0 obj << /Type /Pages /Parent 2 0 R /Kids [5 0 R 6 0 R] /Count 2 >> endobj",
      "4 0 obj << /Type /Pages /Parent 2 0 R /Kids [7 0 R 8 0 R 9 0 R] /Count 3 >> endobj",
      "%%EOF",
    ].join("\n");
    expect(await pdfPageCount(file([text], "scan.pdf"))).toBe(5);
  });

  test("without a catalog, the page tree node without a parent", async () => {
    const text = [
      "%PDF-1.4",
      "3 0 obj << /Type /Pages /Parent 2 0 R /Count 1 >> endobj",
      "2 0 obj << /Count 3 /Type /Pages /Kids [3 0 R] >> endobj",
      "%%EOF",
    ].join("\n");
    expect(await pdfPageCount(file([text], "scan.pdf"))).toBe(3);
  });

  test("a count written as a reference", async () => {
    const text = [
      "%PDF-1.4",
      "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj",
      "2 0 obj << /Type /Pages /Kids [] /Count 12 0 R >> endobj",
      "12 0 obj 3 endobj",
      "%%EOF",
    ].join("\n");
    expect(await pdfPageCount(file([text], "scan.pdf"))).toBe(3);
  });

  test("nothing it can count: no page tree, an empty tree, damaged object stream data, a file too large", async () => {
    expect(await pdfPageCount(file(["%PDF-1.4\n%%EOF\n"], "a.pdf"))).toBeNull();
    const empty = "%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n2 0 obj << /Type /Pages /Count 0 >> endobj\n";
    expect(await pdfPageCount(file([empty], "a.pdf"))).toBeNull();
    const damaged = "%PDF-1.5\n5 0 obj\n<< /Type /ObjStm /N 2 /First 8 /Length 10 /Filter /FlateDecode >>\nstream\n0123456789\nendstream\nendobj\n";
    expect(await pdfPageCount(file([damaged], "a.pdf"))).toBeNull();

    const large = classicPdf(2);
    Object.defineProperty(large, "size", { value: PDF_COUNT_MAX_BYTES + 1 });
    expect(await pdfPageCount(large)).toBeNull();
  });
});

describe("tiffPageCount", () => {
  test("one frame is one page", async () => {
    expect(await tiffPageCount(tiff([{}]))).toBe(1);
  });

  test("each page frame counts; reduced-resolution copies and masks don't", async () => {
    expect(await tiffPageCount(tiff([{}, {}, {}]))).toBe(3);
    expect(await tiffPageCount(tiff([{}, { subfileType: 1 }, {}, { subfileType: 4 }, { subfileType: 2 }]))).toBe(3);
    expect(await tiffPageCount(tiff([{}, { subfileType: 1, short: true }]))).toBe(1);
  });

  test("big-endian", async () => {
    expect(await tiffPageCount(tiff([{}, {}], { bigEndian: true }))).toBe(2);
    expect(await tiffPageCount(tiff([{}, { subfileType: 1, short: true }], { bigEndian: true }))).toBe(1);
  });

  test("more frames than the server looks at count as too many", async () => {
    const frames = Array.from({ length: MAX_TIFF_FRAMES + 3 }, () => ({ subfileType: 1 }));
    expect(await tiffPageCount(tiff(frames))).toBe(MAX_TIFF_FRAMES + 1);
  });

  test("a chain that loops back, or a BigTIFF, isn't counted", async () => {
    expect(await tiffPageCount(tiff([{}, {}], { loop: true }))).toBeNull();
    expect(await tiffPageCount(file([head("II", new Uint8Array([43, 0, 8, 0, 0, 0]))], "big.tif"))).toBeNull();
  });
});

describe("inspectScanFile", () => {
  test("a photo is one page, whatever its name or type says", async () => {
    expect(await inspectScanFile(file([JPEG_HEAD, "x"], "IMG_1.jpg", "image/jpeg"))).toEqual({ kind: "jpeg", pages: 1 });
    expect(await inspectScanFile(file([ftypHead("heic"), "x"], "IMG_2.HEIC"))).toEqual({ kind: "heif", pages: 1 });
    expect(await inspectScanFile(file([PNG_HEAD], "renamed.txt", "text/plain"))).toEqual({ kind: "png", pages: 1 });
  });

  test("a PDF and a TIFF give their pages", async () => {
    expect(await inspectScanFile(classicPdf(2))).toEqual({ kind: "pdf", pages: 2 });
    expect(await inspectScanFile(tiff([{}, {}, {}]))).toEqual({ kind: "tiff", pages: 3 });
    expect(await inspectScanFile(file(["%PDF-1.4\nnot really"], "odd.pdf"))).toEqual({ kind: "pdf", pages: null });
  });

  test("a file the server doesn't read is nothing", async () => {
    expect(await inspectScanFile(file(["2 cups flour"], "notes.txt", "text/plain"))).toBeNull();
    expect(await inspectScanFile(file([head("GIF89a")], "card.gif", "image/gif"))).toBeNull();
    expect(await inspectScanFile(file([JPEG_HEAD.subarray(0, 2)], "cut.jpg", "image/jpeg"))).toBeNull();
  });
});

test("mayBeDocument goes by type and name, for files not read yet", () => {
  expect(mayBeDocument(file(["x"], "a.pdf"))).toBe(true);
  expect(mayBeDocument(file(["x"], "scan.TIFF"))).toBe(true);
  expect(mayBeDocument(file(["x"], "blob", "application/pdf"))).toBe(true);
  expect(mayBeDocument(file(["x"], "IMG_1.jpg", "image/jpeg"))).toBe(false);
  expect(mayBeDocument(new Blob(["x"], { type: "image/jpeg" }))).toBe(false);
});
