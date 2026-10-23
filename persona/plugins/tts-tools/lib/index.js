/** tts-tools —— 用 MiMo 预置音色合成语音并（默认）直接发到当前群/私聊。 */
import fs from 'node:fs';
import { defineTool } from '@deepseek-ai/dsh-tools';
const BOT = process.env.PERSONA_PERCEPTION_BASE || 'http://qqbot-nonebot:8080';
const TOKEN_FILE = process.env.PERSONA_BRIDGE_TOKEN_FILE || '/persona/bridge-token';
export const name = 'persona-tts-tools';
export const inject = ['tools'];
function token(){try{return fs.readFileSync(TOKEN_FILE,'utf8').trim()}catch{return ''}}
export function apply(ctx){
  ctx.tools.register(defineTool({
    name:'tts_speak',
    description:'把一句话合成为语音并直接发到当前群/私聊。\n'+
      '音色可调：冰糖/茉莉（中文女）、苏打/白桦（中文男）、Mia/Chloe（英女）、Milo/Dean（英男）、mimo_default。\n'+
      'style 是自然语言风格（如「温柔、语速稍慢、带一点困意」）；tag 是音频标签（如「(开心)」「(唱歌)」），会放在正文开头。\n'+
      '速度/停顿/情绪没有独立数字参数，用 style/tag 控制；调用后直接发出。',
    parameters:{
      text:{type:'string',description:'要合成的正文，建议 1-4 句'},
      voice:{type:'string',description:'预置音色，默认冰糖'},
      style:{type:'string',description:'自然语言风格，默认温柔轻快女仆语气'},
      tag:{type:'string',description:'可选音频标签，如 (开心)/(唱歌)'},
      scope:{type:'string',description:'群/私聊 scope；省略用本轮 scope'},
    },
    output:{schema:{type:'object',additionalProperties:true,properties:{text:{type:'string'}}},render:(_a,v)=>[{type:'text',text:v.text}]},
    execute:async(args)=>{
      const scope=String(args.scope||process.env.PERSONA_DECISION_SCOPE||'').trim();
      if(!scope) return {text:'拿不到 scope，无法发送语音'};
      const t=token(); if(!t) return {text:'读不到 bridge token'};
      try{
        const resp=await fetch(`${BOT}/tts/say`,{method:'POST',headers:{authorization:`Bearer ${t}`,'content-type':'application/json'},body:JSON.stringify({scope,text:args.text,voice:args.voice,style:args.style,tag:args.tag}),signal:AbortSignal.timeout(100000)});
        const r=await resp.json();
        if(!r.ok) return {text:`语音失败：${r.error||JSON.stringify(r)}`};
        return {text:`语音已发送：${r.view_path}（${r.bytes} bytes，音色 ${r.voice}）`};
      }catch(err){return {text:`语音失败：${String(err)}`}}
    },
  }));
}
