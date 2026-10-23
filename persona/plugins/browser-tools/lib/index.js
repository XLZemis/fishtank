/** browser-tools —— 用感知层常驻 Chromium 打开网页并截图，保存到 /persona/out。 */
import fs from 'node:fs';
import { defineTool } from '@deepseek-ai/dsh-tools';
const BOT = process.env.PERSONA_PERCEPTION_BASE || 'http://qqbot-nonebot:8080';
const TOKEN_FILE = process.env.PERSONA_BRIDGE_TOKEN_FILE || '/persona/bridge-token';
export const name = 'persona-browser-tools';
export const inject = ['tools'];
function token(){try{return fs.readFileSync(TOKEN_FILE,'utf8').trim()}catch{return ''}}
export function apply(ctx){
  ctx.tools.register(defineTool({
    name:'browser_shot',
    description:'打开一个 http(s) 网页并截图，保存到 /persona/out/shot_*.png。\n'+
      '返回的路径可直接用于 read_image 查看，或作为 mail_send 的附件。\n'+
      '参数：url（必填）、full_page（默认 false）、wait_ms（默认 2500，最多 15000）、width/height。\n'+
      '注意：截图是真实浏览器渲染，可能含登录态/隐私内容，发出去前先确认。',
    parameters:{
      url:{type:'string',description:'完整 http(s) URL'},
      full_page:{type:'boolean',description:'是否整页截图'},
      wait_ms:{type:'number',description:'打开后等待毫秒，默认 2500'},
      width:{type:'number',description:'视口宽度，默认 1280'},
      height:{type:'number',description:'视口高度，默认 900'},
    },
    output:{schema:{type:'object',additionalProperties:true,properties:{text:{type:'string'}}},render:(_a,v)=>[{type:'text',text:v.text}]},
    execute:async(args)=>{
      const t=token(); if(!t) return {text:'读不到 bridge token'};
      try{
        const resp=await fetch(`${BOT}/browser/shot`,{method:'POST',headers:{authorization:`Bearer ${t}`,'content-type':'application/json'},body:JSON.stringify({url:args.url,full_page:!!args.full_page,wait_ms:args.wait_ms,width:args.width,height:args.height}),signal:AbortSignal.timeout(70000)});
        const r=await resp.json();
        if(!r.ok) return {text:`截图失败：${r.error||JSON.stringify(r)}`};
        return {text:`截图完成：${r.view_path}\n原图 URL：${r.url}\n大小：${r.bytes} bytes\n（可直接 read_image 查看，或 mail_send 附件）`};
      }catch(err){return {text:`截图失败：${String(err)}`}}
    },
  }));
}
