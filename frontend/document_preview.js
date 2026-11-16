import { renderAsync } from "docx-preview";

window.tassRenderDocumentPreview = async function (url, container, maxHeight) {
  const response = await fetch(url, {
    credentials: "same-origin",
    headers: { Accept: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" },
  });
  if (!response.ok) {
    throw new Error("The document could not be loaded for preview.");
  }

  const declaredSize = Number(response.headers.get("Content-Length") || 0);
  if (declaredSize > 50 * 1024 * 1024) {
    throw new Error("This document is too large to preview.");
  }
  const documentData = await response.arrayBuffer();
  if (documentData.byteLength > 50 * 1024 * 1024) {
    throw new Error("This document is too large to preview.");
  }

  container.replaceChildren();
  container.style.cssText = "max-height:" + (maxHeight || "72vh") + ";min-height:320px;overflow:auto;background:#e8edf4;padding:16px;border:1px solid #dbe2ea;border-radius:10px";
  const styleHost = document.createElement("div");
  const documentHost = document.createElement("div");
  container.append(styleHost, documentHost);

  await renderAsync(documentData, documentHost, styleHost, {
    breakPages: true,
    renderHeaders: true,
    renderFooters: true,
    renderFootnotes: true,
    renderEndnotes: true,
    ignoreLastRenderedPageBreak: false,
  });
  const responsiveStyle = document.createElement("style");
  responsiveStyle.textContent = "@media(max-width:850px){.docx-wrapper{justify-content:flex-start!important;align-items:flex-start!important}}";
  styleHost.appendChild(responsiveStyle);
};
