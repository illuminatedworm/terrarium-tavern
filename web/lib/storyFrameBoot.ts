/** Tiny first document: receive shared immutable Blobs without same-origin access. */
export function frameBoot(token: string) {
  const script = `(()=>{
    const token=${JSON.stringify(token)};
    const receive=async e=>{
      if(e.source!==parent||e.data?.token!==token||e.data.method!=='boot')return;
      removeEventListener('message',receive);
      try {
        const urls=new Map();
        const replace=text=>{for(const [key,url] of urls)text=text.split(key).join(url);return text;};
        for(const item of e.data.assets)if(item.blob.type!=='text/css')urls.set(item.url,URL.createObjectURL(item.blob));
        for(const item of e.data.assets)if(item.blob.type==='text/css')urls.set(item.url,URL.createObjectURL(new Blob([replace(await item.blob.text())],{type:'text/css'})));
        const html=replace(e.data.html);
        document.open();document.write(html);document.close();
        addEventListener('pagehide',()=>{for(const url of urls.values())URL.revokeObjectURL(url);},{once:true});
      }catch(error){parent.postMessage({token,method:'restore-error',args:String(error)},'*');}
    };
    addEventListener('message',receive);parent.postMessage({token,method:'boot'},'*');
  })();`;
  return `<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' blob:; style-src 'unsafe-inline' blob: data:; img-src data: blob:; font-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'"><script>${script}<\/script>`;
}
