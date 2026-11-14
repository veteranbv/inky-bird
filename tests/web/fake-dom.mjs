// A minimal DOM for the Frame unit tests: tree containment, attributes,
// listeners and focus. Events do not bubble; tests dispatch where needed.
export class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parent = null;
    this.attributes = new Map();
    this.listeners = new Map();
    this.classes = new Set();
    this.hidden = false;
    this.disabled = false;
    this.focusVisible = false;
    this.text = '';
    this.classList = {
      contains: (name) => this.classes.has(name),
      remove: (...names) => names.forEach((name) => this.classes.delete(name)),
      add: (...names) => names.forEach((name) => this.classes.add(name)),
      toggle: (name, force) => {
        const on = force === undefined ? !this.classes.has(name) : force;
        if (on) this.classes.add(name);
        else this.classes.delete(name);
        return on;
      },
    };
  }
  get className() {
    return [...this.classes].join(' ');
  }
  set className(value) {
    this.classes = new Set(value.split(' ').filter(Boolean));
  }
  get textContent() {
    return this.children.length ? this.children.map((node) => node.textContent).join('') : this.text;
  }
  set textContent(value) {
    this.children = [];
    this.text = String(value);
  }
  append(...nodes) {
    for (const node of nodes) {
      node.parent?.children.splice(node.parent.children.indexOf(node), 1);
      node.parent = this;
      this.children.push(node);
    }
  }
  replaceChildren(...nodes) {
    for (const node of this.children) node.parent = null;
    this.children = [];
    this.text = '';
    this.append(...nodes);
  }
  remove() {
    if (!this.parent) return;
    this.parent.children.splice(this.parent.children.indexOf(this), 1);
    this.parent = null;
  }
  contains(node) {
    for (let item = node; item; item = item.parent) if (item === this) return true;
    return false;
  }
  closest(selector) {
    const tags = selector.split(',').map((part) => part.trim().toUpperCase());
    for (let item = this; item; item = item.parent) if (tags.includes(item.tagName)) return item;
    return null;
  }
  // Supports `tag`, `.class` and `#id`, searched depth first.
  querySelector(selector) {
    for (const child of this.children) {
      if (
        (selector.startsWith('.') && child.classes.has(selector.slice(1))) ||
        (selector.startsWith('#') && child.id === selector.slice(1)) ||
        child.tagName === selector.toUpperCase()
      )
        return child;
      const nested = child.querySelector(selector);
      if (nested) return nested;
    }
    return null;
  }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }
  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }
  removeAttribute(name) {
    this.attributes.delete(name);
    if (name === 'src') delete this.src;
  }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(listener);
  }
  removeEventListener(type, listener) {
    this.listeners.get(type)?.delete(listener);
  }
  fire(type, init = {}) {
    const event = {
      type,
      target: this,
      isPrimary: true,
      pointerType: 'touch',
      defaultPrevented: false,
      stopped: false,
      preventDefault() {
        this.defaultPrevented = true;
      },
      stopPropagation() {
        this.stopped = true;
      },
      ...init,
    };
    for (const listener of [...(this.listeners.get(type) || [])]) listener(event);
    return event;
  }
  click() {
    if (!this.disabled) this.fire('click');
  }
  focus() {
    globalThis.document.activeElement = this;
  }
  blur() {
    if (globalThis.document.activeElement === this)
      globalThis.document.activeElement = globalThis.document.body;
  }
  matches(selector) {
    return (
      selector === ':focus-visible' && this.focusVisible && globalThis.document.activeElement === this
    );
  }
}

export function fakeDocument() {
  const doc = new Element('#document');
  doc.hidden = false;
  doc.title = '';
  doc.body = new Element('body');
  doc.append(doc.body);
  doc.activeElement = doc.body;
  doc.documentElement = {};
  doc.createElement = (tag) => new Element(tag);
  doc.createElementNS = (_namespace, tag) => new Element(tag);
  return doc;
}

// The first button whose text is `text`, anywhere under `root`.
export function findButton(root, text) {
  for (const child of root.children) {
    if (child.tagName === 'BUTTON' && child.textContent === text) return child;
    const nested = findButton(child, text);
    if (nested) return nested;
  }
  return null;
}
