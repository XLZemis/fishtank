/**
 * persona-doc-tools —— 给Persona一个文档解析工具（doc_read）
 *
 * 依赖容器里的 /usr/local/bin/doc2text（Python 实现）：
 *   PDF / DOCX / DOC / XLSX / PPTX / ODT / HTML / CSV / TXT / MD / JSON ...
 * 只读取本地文件，输出纯文本；扫描版 PDF 不做 OCR。
 */

import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { defineTool } from '@deepseek-ai/dsh-tools';

const run = promisify(execFile);
export const name = 'persona-doc-tools';
export const inject = ['tools'];

const DESC = `读取文档并转成纯文本。支持 PDF、DOCX、DOC、XLSX/XLSM、PPTX、ODT/ODS/ODP、
HTML、CSV/TSV、TXT/MD/JSON/XML/RTF。适合看群文件、资料、表格、幻灯片；图片请用 read_image。
参数 path 是文件路径（例如 /persona/out/xxx.pdf 或 /persona/work/xxx.docx）；
max_chars 默认 12000，最大 50000，超出会截断。扫描版 PDF 只提取文本层，不做 OCR。`;

export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: 'doc_read',
    description: DESC,
    parameters: {
      path: { type: 'string', description: '要读取的文件路径（绝对路径或相对 /persona）' },
      max_chars: { type: 'number', description: '最多返回多少字符，默认 12000，上限 50000' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_args, value) => [{ type: 'text', text: value.text }],
    },
    execute: async (args) => {
      const path = String(args.path || '').trim();
      if (!path) return { text: '缺少 path。' };
      const max = Math.max(1000, Math.min(Number(args.max_chars) || 12000, 50000));
      try {
        const { stdout, stderr } = await run('/usr/local/bin/doc2text', [path, String(max)], {
          timeout: 60_000,
          maxBuffer: 4 * 1024 * 1024,
        });
        const text = String(stdout || '').trim();
        if (!text) return { text: (stderr || '').trim() || '文档里没有提取到文本。' };
        return { text };
      } catch (err) {
        const msg = String(err?.stderr || err?.message || err).trim();
        return { text: `文档解析失败：${msg || 'doc2text 执行失败'}` };
      }
    },
  }));
}
