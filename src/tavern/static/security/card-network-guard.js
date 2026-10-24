(() => {
  // Installed by the browser host before *every* document, not by the card's
  // first script. CSP/connect-src and HTTP interception do not block ICE/STUN.
  if (Object.getOwnPropertyDescriptor(globalThis, '__tavernScriptIsolation')?.value === 1) return;
  for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection'])
    Object.defineProperty(globalThis, name, {
      value: class {
        constructor() {
          throw Error('卡片不能直接访问网络');
        }
      },
      writable: false,
      configurable: false,
    });
  Object.defineProperty(globalThis, '__tavernScriptIsolation', {
    value: 1,
    writable: false,
    configurable: false,
  });
})();
