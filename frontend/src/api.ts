export type Project = {id:string;name:string;repo:string;branch:string|null;sha:string|null;registered_at:string|null;obsidian_note:string|null;graph:{ready:boolean;nodes:number;edges:number;updated:number|null;html:boolean;report:boolean};knowledge:{count:number;systems:number;adrs:number;incidents:number;recent:{title:string;path:string}[]}|null};
export type Preview = {repo:string;name:string;git:{is_git:boolean;branch:string|null;sha:string|null};graphify_installed:boolean;graph_ready:boolean;existing_knowledge:string|null;vault_configured:boolean};
export type Op = {id:string;status:'running'|'complete'|'failed';events:{time:string;step:string;status:string;detail?:unknown}[]};
let csrf = '';
export function setCsrf(value:string){csrf=value}
export async function api<T>(path:string, method='GET', body?:unknown):Promise<T>{
  const response=await fetch('/api'+path,{method,credentials:'same-origin',headers:{...(method!=='GET'?{'Content-Type':'application/json','X-Hub-CSRF':csrf}: {})},body:body===undefined?undefined:JSON.stringify(body)});
  if(!response.ok){let message=`Request failed (${response.status})`;try{const data=await response.json();message=data.detail||message}catch{}throw new Error(message)}
  return response.json() as Promise<T>;
}
export async function session(){const r=await fetch('/api/session',{credentials:'same-origin'});return r.json() as Promise<{authenticated:boolean;csrf:string|null}>}
export async function login(token:string){const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});if(!r.ok)throw new Error('Invalid access key');const data=await r.json();setCsrf(data.csrf);return data}
