/** mail-tools —— 通过 SMTP 发邮件，可带 /persona 下的附件（如 browser_shot 截图）。 */
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { defineTool } from '@deepseek-ai/dsh-tools';
const run = promisify(execFile);
export const name = 'persona-mail-tools';
export const inject = ['tools'];
export function apply(ctx){
  ctx.tools.register(defineTool({
    name:'mail_send',
    description:'发邮件（SMTP）。to 可以是字符串或字符串数组；attachments 是 /persona 下的文件路径数组。\n'+
      '需要部署侧配置：FISHTANK_SMTP_HOST/PORT/USER/PASSWORD/FROM；可选 SECURITY=ssl|starttls|plain（默认按端口）。\n'+
      '示例：把 browser_shot 的 /persona/out/shot_xxx.png 作为附件发出去。',
    parameters:{
      to:{type:'string',description:'收件人，多个用逗号分隔'},
      subject:{type:'string',description:'主题'},
      body:{type:'string',description:'正文'},
      attachments:{type:'array',items:{type:'string'},description:'可选附件路径（仅 /persona 下）'},
    },
    output:{schema:{type:'object',additionalProperties:true,properties:{text:{type:'string'}}},render:(_a,v)=>[{type:'text',text:v.text}]},
    execute:async(args)=>{
      const payload={to:String(args.to||''),subject:String(args.subject||''),body:String(args.body||''),attachments:Array.isArray(args.attachments)?args.attachments:[]};
      try{
        const {stdout,stderr}=await run('/usr/bin/python3',['/persona/tools/mail_send.py',JSON.stringify(payload)],{timeout:90000,maxBuffer:1024*1024});
        const parsed=JSON.parse(String(stdout||'{}'));
        if(!parsed.ok) return {text:`邮件发送失败：${parsed.error||stderr||'unknown'}`};
        return {text:`邮件已发送：${parsed.to}\n附件：${(parsed.attachments||[]).join(', ')||'无'}`};
      }catch(err){
        let detail=String(err?.stderr||err?.message||err);
        try{const p=JSON.parse(String(err?.stdout||'{}')); if(p.error) detail=p.error;}catch{}
        return {text:`邮件发送失败：${detail}`};
      }
    },
  }));
}
