/** recall-tools —— 撤回Persona自己最近发出的消息（只认感知层发送缓存）。 */
import fs from 'node:fs';
import { defineTool } from '@deepseek-ai/dsh-tools';
const BOT = process.env.PERSONA_PERCEPTION_BASE || 'http://qqbot-nonebot:8080';
const TOKEN_FILE = process.env.PERSONA_BRIDGE_TOKEN_FILE || '/persona/bridge-token';
export const name = 'persona-recall-tools';
export const inject = ['tools'];
function token(){try{return fs.readFileSync(TOKEN_FILE,'utf8').trim()}catch{return ''}}
export function apply(ctx){
  ctx.tools.register(defineTool({
    name:'message_recall',
    description:'撤回Persona自己最近发出的消息（默认 1 条，最多 10 条）。\n'+
      '只会撤回感知层记录过的“她刚发的”消息，不能撤回别人的消息。\n'+
      'QQ 普通成员通常只有 2 分钟窗口；超时会返回错误。',
    parameters:{
      scope:{type:'string',description:'群/私聊 scope；省略用本轮 scope'},
      count:{type:'number',description:'撤回最近几条，默认 1，最多 10'},
      message_id:{type:'string',description:'可选：指定最近发送记录里的 message_id'},
    },
    output:{schema:{type:'object',additionalProperties:true,properties:{text:{type:'string'}}},render:(_a,v)=>[{type:'text',text:v.text}]},
    execute:async(args)=>{
      const scope=String(args.scope||process.env.PERSONA_DECISION_SCOPE||'').trim();
      if(!scope) return {text:'拿不到 scope，无法撤回'};
      const t=token(); if(!t) return {text:'读不到 bridge token'};
      try{
        const resp=await fetch(`${BOT}/message/recall`,{method:'POST',headers:{authorization:`Bearer ${t}`,'content-type':'application/json'},body:JSON.stringify({scope,count:args.count,message_id:args.message_id}),signal:AbortSignal.timeout(15000)});
        const r=await resp.json();
        if(!r.ok) return {text:`撤回失败：${(r.errors||[]).join('; ')||r.error||JSON.stringify(r)}`};
        const lines=(r.recalled||[]).map(x=>`#${x.message_id} ${x.kind} ${x.text||''}`.trim());
        return {text:`已撤回：\n${lines.join('\n')}`};
      }catch(err){return {text:`撤回失败：${String(err)}`}}
    },
  }));
}
