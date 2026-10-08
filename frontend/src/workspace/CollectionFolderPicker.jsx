import {Folder} from 'lucide-react';
import {folderLabel} from './collectionFolders.mjs';

export function CollectionFolderPicker({folders=[],value='',onChange,busy=false,error='',onRetry}){
  return <div className="collection-folder-picker">
    <label><Folder size={14}/><span>收藏夹</span><select aria-label="选择具体收藏夹" value={value} onChange={e=>onChange(e.target.value)}>
      <option value="">全部收藏夹</option>
      {value&&!folders.some(f=>f.id===value)&&<option value={value}>当前收藏夹</option>}
      {folders.map(f=><option value={f.id} key={f.id}>{folderLabel(f)}</option>)}
    </select></label>
    {busy&&!folders.length&&<small role="status">正在读取收藏夹…</small>}
    {error&&<small role="alert">收藏夹名称未能读取。<button onClick={onRetry}>重试</button></small>}
    {!busy&&!error&&!folders.length&&<small>尚未添加指定收藏夹，可到同步来源添加。</small>}
  </div>;
}
