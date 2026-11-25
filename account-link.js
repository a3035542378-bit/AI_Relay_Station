(() => {
  function addLink() {
    const heading = document.querySelector('#tab-account h2');
    if (!heading || document.querySelector('#office-personal-models-link')) return;
    const link = document.createElement('a');
    link.id = 'office-personal-models-link';
    link.setAttribute('data-sveltekit-reload', '');
    link.href = '/personal-models';
    link.textContent = '管理我的 API 和模型 →';
    link.style.cssText = 'display:inline-block;margin-bottom:1rem;text-decoration:underline';
    heading.after(link);
  }
  new MutationObserver(addLink).observe(document.body, {childList:true, subtree:true});
  addLink();
})();
