// Product navigation, not persisted business entities or inferred knowledge links.
export const topNavigation = [
  ['workspace','工作台','WORKSPACE'], ['works','作品库','WORKS'],
  ['drafts','创作台','STUDIO'], ['ideas','选题池','IDEAS'], ['library','收藏库','LIBRARY'],
];
export const modules = [
  {id:'concepts',name:'概念网络',english:'Concepts',color:'#c9d2de',planned:true,tagline:'把零散理解，连成自己的知识网络。',purpose:'整理主题、概念与内容之间的明确关联。',input:'自己的笔记、收藏里的概念',output:'概念说明与关联内容',relation:'关联概念时保留原始资料与自己的解释，推荐关联需另行确认。'},
  {id:'methods',name:'方法库',english:'Methods',color:'#b0c8df',planned:true,tagline:'把做过的方法，留给下一次创作。',purpose:'沉淀可复用的步骤、提示词和工作流。',input:'实践记录、教程和提示词',output:'可复用的方法与步骤',relation:'方法可以被多篇创作引用，收藏教程与自己的实践结论分开保存。'},
  {id:'works',name:'作品库',english:'Works',color:'#c4bedb',planned:true,tagline:'让每一次创作，都成为自己的积累。',purpose:'整理自己完成的内容与平台版本。',input:'完成的草稿、图文或视频',output:'自己的作品与版本记录',relation:'保留对应选题、创作过程与引用资料；这里不收录他人的收藏作品。'},
  {id:'ideas',name:'选题池',english:'Ideas',color:'#d6cfbe',planned:true,tagline:'先留住想法，再找到值得讲的角度。',purpose:'收集灵感，明确受众、问题与表达角度。',input:'随手灵感、资料或具体问题',output:'待创作的选题与参考清单',relation:'一个选题可关联多条收藏，也可以直接从自己的想法开始。'},
  {id:'drafts',name:'创作台',english:'Studio',color:'#adc5d9',planned:true,tagline:'从一个想法，走到一份完整表达。',purpose:'编写正文、脚本，组织版本与制作附件。',input:'已有选题，或直接开始一份草稿',output:'草稿、制作附件与完成的作品',relation:'写作时引用收藏、概念或方法，完成后归入作品库。'},
  {id:'library',name:'收藏库',english:'Library',color:'#c3d5ee',planned:false,tagline:'留住外部灵感，随时找回。'},
];
export function parseRoute(hash='') {
  const parts=hash.replace(/^#/,'').split('/').map(value=>{try{return decodeURIComponent(value)}catch{return ''}});
  const [first='workspace',second='',third=''] = parts;
  const page = [...topNavigation.map(x=>x[0]),'concepts','methods','sync','ai','settings','tasks'].includes(first)?first:'workspace';
  if(page==='workspace') return {page,module:modules.some(x=>x.id===second)?second:'',id:second==='library'?third:''};
  return {page,module:'',id:['library','ai','settings','tasks'].includes(page)?second:'',...(page==='library'&&third==='read'?{expanded:true}:{}),...(page==='library'&&['summary','audio','screen'].includes(third)?{tab:third}:{})};
}
export function expandedReadingHash(id){return '#library/'+encodeURIComponent(id)+'/read';}
export function activeNavigation(page){return page==='sync'?'library':['concepts','methods'].includes(page)?'workspace':page;}
