import assert from 'node:assert/strict';
import test from 'node:test';

class TextElement {
    constructor(tagName) {
        this.tagName = String(tagName || '').toLowerCase();
        this._innerHTML = '';
        this._textContent = '';
        this.value = '';
    }

    set textContent(value) {
        this._textContent = String(value ?? '');
        this._innerHTML = this._textContent
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;');
    }

    get textContent() { return this._textContent; }

    set innerHTML(value) {
        this._innerHTML = String(value ?? '');
        if (this.tagName === 'textarea') {
            this.value = this._innerHTML
                .replace(/&lt;/gi, '<')
                .replace(/&gt;/gi, '>')
                .replace(/&quot;/gi, '"')
                .replace(/&#0*39;/g, "'")
                .replace(/&#x0*27;/gi, "'")
                .replace(/&amp;/gi, '&');
        }
    }

    get innerHTML() { return this._innerHTML; }
}

const priorDocument = globalThis.document;
globalThis.document = { createElement: (tagName) => new TextElement(tagName) };
const { renderMarkdown, renderMarkdownSafe } = await import('../modules/utils.js');

test.after(() => { globalThis.document = priorDocument; });

test('legacy renderMarkdown pins GFM-style pipe-table shape and quirks', () => {
    const source = [
        '| First | Second |',
        '| :--- | ---: |',
        '| one | two |',
        '| three | four |',
    ].join('\n');
    assert.equal(
        renderMarkdown(source),
        '<div class="md-table-wrap"><table class="md-table"><thead><tr><th>First</th><th>Second</th></tr></thead><tbody><tr><td>one</td><td>two</td></tr><tr><td>three</td><td>four</td></tr></tbody></table></div>',
    );
    assert.equal(renderMarkdown('| lone | row |'), '| lone | row |');
});

test('legacy renderMarkdown routes links through safeExternalUrl', () => {
    assert.equal(
        renderMarkdown('[Web](https://example.com/docs) [Mail](mailto:owner@example.com) [Bad](javascript:alert(1))'),
        '<a href="https://example.com/docs" target="_blank" rel="noopener noreferrer" class="md-link">Web</a> <a href="mailto:owner@example.com" target="_blank" rel="noopener noreferrer" class="md-link">Mail</a> <a href="#" target="_blank" rel="noopener noreferrer" class="md-link">Bad</a>)',
    );
});

test('legacy renderMarkdown pins emphasis, inline code, and fake headings', () => {
    assert.equal(
        renderMarkdown('# One\n## Two\n### Three\n**bold** *italic* ~~strike~~ `code`'),
        '<strong class="md-h1">One</strong>\n<strong class="md-h2">Two</strong>\n<strong class="md-h3">Three</strong>\n<strong>bold</strong> <em>italic</em> <del>strike</del> <code class="inline-code">code</code>',
    );
});

test('legacy renderMarkdown discards fenced-code language', () => {
    assert.equal(
        renderMarkdown('```javascript\nconst less = 1 < 2;\n```'),
        '<pre><code>const less = 1 &lt; 2;\n</code></pre>',
    );
});

test('legacy renderMarkdown escapes raw and stored entity text without decoding', () => {
    assert.equal(renderMarkdown('<b>& raw</b>'), '&lt;b&gt;&amp; raw&lt;/b&gt;');
    assert.equal(renderMarkdown('&lt;b&gt;&amp;amp;'), '&amp;lt;b&amp;gt;&amp;amp;amp;');
    assert.equal(renderMarkdown('`<tag>&`'), '<code class="inline-code">&lt;tag&gt;&amp;</code>');
});

test('renderMarkdownSafe exposes escaped pre/code fallback without DOMPurify', () => {
    const priorMarked = globalThis.marked;
    const priorPurify = globalThis.DOMPurify;
    globalThis.marked = { parse: () => '<p>must not be used</p>' };
    globalThis.DOMPurify = undefined;
    try {
        assert.equal(
            renderMarkdownSafe('<script>alert(1)</script> & text', { preClass: 'publisher-md' }),
            '<pre class="publisher-md"><code>&lt;script&gt;alert(1)&lt;/script&gt; &amp; text</code></pre>',
        );
    } finally {
        globalThis.marked = priorMarked;
        globalThis.DOMPurify = priorPurify;
    }
});
