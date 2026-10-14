export function nextAgentCode(agents, prefix='deepseek') {
  const names=new Set(agents.map(agent=>agent.name));
  let number=1;
  while(names.has(`${prefix}-${number}`)) number++;
  return `${prefix}-${number}`;
}
