/** napcat-tools —— 把 NapCat/OneBot 的只读查询投射成工具，不暴露写操作。 */
import fs from 'node:fs';
import { defineTool } from '@deepseek-ai/dsh-tools';
const BOT = process.env.PERSONA_PERCEPTION_BASE || 'http://qqbot-nonebot:8080';
const TOKEN_FILE = process.env.PERSONA_BRIDGE_TOKEN_FILE || '/persona/bridge-token';
export const name = 'persona-napcat-tools';
export const inject = ['tools'];
function token(){try{return fs.readFileSync(TOKEN_FILE,'utf8').trim()}catch{return ''}}
const ALLOWED = [
  'get_msg','get_forward_msg','get_group_info','get_group_list','get_group_member_info',
  'get_group_member_list','get_friend_list','get_stranger_info','get_status','get_version_info',
  'get_login_info','can_send_image','can_send_record',
];
export function apply(ctx){
  ctx.tools.register(defineTool({
    name:'napcat_query',
    description:'只读调用 NapCat/OneBot 查询 API（不发送消息、不改群）。\n'+
      '允许：'+ALLOWED.join(', ')+'。\n'+
      'params 是 API 参数对象，例如 get_group_member_info {group_id:"...", user_id:"..."}。\n'+
      '需要发消息/戳一戳请用 sentence/interact 工具，不要用这个。',
    parameters:{
      api:{type:'string',description:'白名单内的 OneBot API 名'},
      params:{type:'object',additionalProperties:true,description:'API 参数对象，默认空'},
    },
    output:{schema:{type:'object',additionalProperties:true,properties:{text:{type:'string'}}},render:(_a,v)=>[{type:'text',text:v.text}]},
    execute:async(args)=>{
      const api=String(args.api||'').trim();
      if(!ALLOWED.includes(api)) return {text:`api 不在只读白名单：${api}`};
      const t=token(); if(!t) return {text:'读不到 bridge token'};
      try{
        const resp=await fetch(`${BOT}/onebot/query`,{method:'POST',headers:{authorization:`Bearer ${t}`,'content-type':'application/json'},body:JSON.stringify({api,params:args.params||{}}),signal:AbortSignal.timeout(20000)});
        const r=await resp.json();
        if(!r.ok) return {text:`查询失败：${r.error||JSON.stringify(r)}`};
        return {text:`${api} 返回：\n${JSON.stringify(r.data,null,2).slice(0,12000)}`};
      }catch(err){return {text:`查询失败：${String(err)}`}}
    },
  }));
}
