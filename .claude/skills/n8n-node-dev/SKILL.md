---
name: n8n-node-dev
description: "Build, scaffold, test, and publish custom n8n nodes and integrations (TypeScript). Use this skill whenever the user asks to create a custom n8n node, build an n8n integration for an API, add a new node to n8n, write a trigger node, webhook node, polling node, or AI sub-node (Tool/Embeddings/Vector Store/LLM) for n8n. Also triggers for: 'how do I add X to n8n', 'create a community node', 'build n8n integration', 'n8n node development', packaging or publishing n8n community nodes."
---

# n8n Custom Node Development

Build production-grade n8n nodes in TypeScript — action nodes, polling/webhook triggers, and AI/LangChain sub-nodes.

## The single load-bearing fact

An n8n node is a **TypeScript class implementing `INodeType`** from `n8n-workflow`, compiled to CommonJS, and packaged as an npm module whose `package.json` declares `n8n.nodes` and `n8n.credentials` paths pointing at compiled `.js` files in `dist/`. Everything else is variation on that theme.

---

## 1. Bootstrap

```bash
npm create @n8n/node@latest n8n-nodes-myapi
# Choose template: declarative/custom or programmatic/example
cd n8n-nodes-myapi
npm install
npm run dev       # hot-reload n8n instance at localhost:5678
```

**Legacy `n8n-node-dev` is deprecated** — use `@n8n/node-cli` exclusively.

### Required `package.json` fields

```json
{
  "name": "n8n-nodes-myapi",
  "keywords": ["n8n-community-node-package"],
  "license": "MIT",
  "main": "index.js",
  "files": ["dist"],
  "n8n": {
    "n8nNodesApiVersion": 1,
    "nodes": ["dist/nodes/MyApi/MyApi.node.js"],
    "credentials": ["dist/credentials/MyApi.credentials.js"]
  },
  "peerDependencies": { "n8n-workflow": "*" }
}
```

**Hard rules:**
- `name` must start with `n8n-nodes-` or `@scope/n8n-nodes-`
- `n8nNodesApiVersion` must be the **number** `1`, not the string `"1"`
- `n8n-workflow` is a **peerDependency only** — bundling it causes silent `instanceof` failures
- Paths in `n8n.nodes`/`n8n.credentials` reference compiled `.js` in `dist/`, never `.ts`

### `tsconfig.json` essentials

```json
{
  "compilerOptions": {
    "module": "commonjs", "target": "es2019",
    "outDir": "./dist/", "strict": true,
    "resolveJsonModule": true, "esModuleInterop": true,
    "useUnknownInCatchVariables": false
  }
}
```

---

## 2. Node kinds

| Kind | Method | Group | Notes |
|---|---|---|---|
| Action (programmatic) | `execute()` | `transform` | Most common |
| Action (declarative) | none — uses `routing` | `transform` | REST-only APIs |
| Polling trigger | `poll()` | `trigger` + `polling: true` | Cursor-based |
| Webhook trigger | `webhook()` + `webhooks: [...]` | `trigger` | Receives HTTP |
| AI sub-node | `supplyData()` | `transform` | LangChain primitives |

---

## 3. Minimal action node

```ts
import {
  IExecuteFunctions, INodeExecutionData, INodeType, INodeTypeDescription,
  NodeConnectionType, NodeApiError, JsonObject,
} from 'n8n-workflow';

export class MyApi implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'My API', name: 'myApi', icon: 'file:myapi.svg',
    group: ['transform'], version: 1,
    subtitle: '={{$parameter["operation"] + ": " + $parameter["resource"]}}',
    description: 'Interact with MyAPI', defaults: { name: 'My API' },
    inputs: [NodeConnectionType.Main], outputs: [NodeConnectionType.Main],
    credentials: [{ name: 'myApi', required: true }],
    properties: [
      {
        displayName: 'Resource', name: 'resource', type: 'options',
        noDataExpression: true,                    // ← REQUIRED on resource/operation
        options: [{ name: 'Contact', value: 'contact' }], default: 'contact',
      },
      {
        displayName: 'Operation', name: 'operation', type: 'options',
        noDataExpression: true,
        displayOptions: { show: { resource: ['contact'] } },
        options: [{ name: 'Create', value: 'create', action: 'Create a contact' }],
        default: 'create',
      },
      {
        displayName: 'Email', name: 'email', type: 'string', required: true,
        default: '',                               // ← REQUIRED even when empty string
        displayOptions: { show: { resource: ['contact'], operation: ['create'] } },
      },
    ],
  };

  async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
    const items = this.getInputData();
    const returnData: INodeExecutionData[] = [];

    for (let i = 0; i < items.length; i++) {
      try {
        const email = this.getNodeParameter('email', i) as string; // ← pass i, not 0
        const response = await this.helpers.httpRequestWithAuthentication.call(
          this, 'myApi',
          { method: 'POST', url: 'https://api.example.com/contacts', body: { email }, json: true },
        );
        returnData.push(...this.helpers.constructExecutionMetaData(
          this.helpers.returnJsonArray(response),
          { itemData: { item: i } },              // ← pairedItem: REQUIRED
        ));
      } catch (error) {
        if (this.continueOnFail()) {
          returnData.push({ json: { error: (error as Error).message }, pairedItem: { item: i } });
          continue;
        }
        throw new NodeApiError(this.getNode(), error as JsonObject, { itemIndex: i });
      }
    }
    return [returnData];
  }
}
```

**Critical gotchas:**
1. Always pass the loop variable `i` to `getNodeParameter` — passing `0` breaks per-row expression resolution
2. Always set `pairedItem` via `constructExecutionMetaData` — without it, `$('Node').item` breaks downstream
3. `default` is required on **every** property
4. `noDataExpression: true` is required on every `resource`/`operation` dropdown (linter enforces this)

---

## 4. Credentials

```ts
// credentials/MyApi.credentials.ts
export class MyApi implements ICredentialType {
  name = 'myApi';
  displayName = 'My API';
  properties: INodeProperties[] = [
    { displayName: 'API Key', name: 'apiKey', type: 'string',
      typeOptions: { password: true }, default: '', required: true },
  ];
  authenticate: IAuthenticateGeneric = {
    type: 'generic',
    properties: { headers: { Authorization: '=Bearer {{$credentials.apiKey}}' } },
  };
  test: ICredentialTestRequest = {
    request: { baseURL: 'https://api.example.com', url: '/me' },
  };
}
```

For OAuth2, `extends = ['oAuth2Api']` and set `authUrl`, `accessTokenUrl`, `scope` as hidden properties. `httpRequestWithAuthentication` auto-refreshes tokens.

---

## 5. Trigger nodes

### Polling trigger

```ts
description = {
  group: ['trigger'], polling: true, inputs: [], outputs: [NodeConnectionType.Main],
  // ...
};

async poll(this: IPollFunctions): Promise<INodeExecutionData[][] | null> {
  const staticData = this.getWorkflowStaticData('node');
  const isManual = this.getMode() === 'manual';
  const since = (staticData.lastTimeChecked as string) ?? new Date(0).toISOString();

  const items = await this.helpers.httpRequestWithAuthentication.call(this, 'myApi',
    { method: 'GET', url: 'https://api.example.com/items',
      qs: isManual ? { limit: 1 } : { since }, json: true });

  if (!isManual) staticData.lastTimeChecked = new Date().toISOString();
  if (!items?.length) return null;
  return [this.helpers.returnJsonArray(items)];
}
```

**Rule:** Don't advance cursor when `getMode() === 'manual'` — manual test runs don't persist staticData.

### Webhook trigger (with lifecycle hooks)

```ts
description = {
  group: ['trigger'], inputs: [], outputs: [NodeConnectionType.Main],
  webhooks: [{ name: 'default', httpMethod: 'POST', responseMode: 'onReceived',
               responseCode: 200, path: 'webhook' }],
};

webhookMethods = {
  default: {
    async checkExists(this: IHookFunctions): Promise<boolean> { /* GET hook from API */ },
    async create(this: IHookFunctions): Promise<boolean> {
      const url = this.getNodeWebhookUrl('default') as string;
      const res = await this.helpers.httpRequestWithAuthentication.call(this, 'myApi',
        { method: 'POST', url: 'https://api.example.com/hooks', body: { url }, json: true });
      this.getWorkflowStaticData('node').webhookId = res.id;
      return true;
    },
    async delete(this: IHookFunctions): Promise<boolean> { /* DELETE hook from API */ },
  },
};

async webhook(this: IWebhookFunctions): Promise<IWebhookResponseData> {
  return { workflowData: [this.helpers.returnJsonArray(this.getBodyData())] };
}
```

`responseMode`: `'onReceived'` = async (best for app webhooks); `'lastNode'` = wait for workflow; `'responseNode'` = defer to Respond-to-Webhook node.

---

## 6. AI sub-nodes (`supplyData`)

```ts
import { DynamicStructuredTool } from '@langchain/core/tools';
import { z } from 'zod';
import { NodeConnectionType } from 'n8n-workflow';

export class MyTool implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'My Tool', name: 'myTool', icon: 'file:tool.svg',
    group: ['transform'], version: 1, defaults: { name: 'My Tool' },
    codex: { categories: ['AI'], subcategories: { AI: ['Tools'], Tools: ['Other Tools'] } },
    inputs: [], outputs: [NodeConnectionType.AiTool], outputNames: ['Tool'],
    properties: [{ displayName: 'Description', name: 'toolDescription',
                   type: 'string', default: 'Describe what this tool does' }],
  };

  async supplyData(this: ISupplyDataFunctions, itemIndex: number): Promise<SupplyData> {
    const description = this.getNodeParameter('toolDescription', itemIndex) as string;
    return {
      response: new DynamicStructuredTool({
        name: 'myTool', description,
        schema: z.object({ query: z.string().describe('The search query') }),
        func: async ({ query }) => {
          // perform action, return string
          return `Result for: ${query}`;
        },
      }),
    };
  }
}
```

**AI sub-node rules:**
- Schema must be **Zod** — JSON Schema is silently dropped by `DynamicStructuredTool`
- For zero-param tools, use `DynamicTool` (string in/out) — empty Zod schemas break Gemini
- `codex.subcategories` controls AI palette placement — always set it
- `usableAsTool: true` on action nodes auto-wraps them for AI Agent (requires `N8N_COMMUNITY_PACKAGES_ALLOW_TOOL_USAGE=true` for community packages)

**Consuming upstream sub-nodes:**
```ts
const embeddings = (await this.getInputConnectionData(
  NodeConnectionType.AiEmbedding, itemIndex)) as Embeddings;
```

---

## 7. Declarative style (REST-only)

```ts
// No execute() needed — n8n handles the HTTP call
properties: [
  { displayName: 'Date', name: 'date', type: 'dateTime', default: '',
    routing: { request: { qs: { date: '={{ new Date($value).toISOString().substring(0,10) }}' } } } },
],
requestDefaults: { baseURL: 'https://api.example.com', headers: { Accept: 'application/json' } },
```

Use declarative only for pure REST. Not suitable for: GraphQL, complex transforms, full versioning, or dynamic `loadOptions`.

---

## 8. Dynamic UI

### `loadOptions` — dynamic dropdowns

```ts
properties: [{
  displayName: 'Project Name or ID', name: 'projectId', type: 'options',
  typeOptions: { loadOptionsMethod: 'getProjects', loadOptionsDependsOn: ['workspaceId'] },
  default: '',
  description: 'Choose from the list, or specify an ID using an <a href="https://docs.n8n.io/code/expressions/">expression</a>.',
}],
methods = {
  loadOptions: {
    async getProjects(this: ILoadOptionsFunctions): Promise<INodePropertyOptions[]> {
      const ws = this.getCurrentNodeParameter('workspaceId') as string;
      const data = await apiRequest.call(this, 'GET', `/workspaces/${ws}/projects`);
      return data.map(p => ({ name: p.name, value: p.id }));
    },
  },
};
```

### `resourceLocator` — list + URL + ID picker

```ts
{ displayName: 'Repo', name: 'repo', type: 'resourceLocator',
  default: { mode: 'list', value: '' },
  modes: [
    { displayName: 'From List', name: 'list', type: 'list',
      typeOptions: { searchListMethod: 'getRepos', searchable: true } },
    { displayName: 'By URL', name: 'url', type: 'string',
      extractValue: { type: 'regex', regex: 'https:\\/\\/github\\.com\\/[^/]+\\/([-\\w]+)' } },
    { displayName: 'By Name', name: 'name', type: 'string' },
  ] }
// In execute: this.getNodeParameter('repo', i, undefined, { extractValue: true })
```

---

## 9. Node versioning

**Lightweight** (different properties per version, same execute):
```ts
version: [1, 2], defaultVersion: 2
// Gate properties with: displayOptions: { show: { '@version': [{ _cnd: { gte: 2 } }] } }
```

**Full** (different execute logic per version):
```ts
export class MyService extends VersionedNodeType {
  constructor() {
    const base = { displayName: 'My Service', name: 'myService', defaultVersion: 2, ... };
    super({ 1: new MyServiceV1(base), 2: new MyServiceV2(base) }, base);
  }
}
```

---

## 10. Error handling

```ts
// NodeOperationError → internal/validation failures
throw new NodeOperationError(this.getNode(), 'Invalid email', {
  itemIndex: i, description: 'Provide a valid email like name@example.com' });

// NodeApiError → third-party API failures
throw new NodeApiError(this.getNode(), error as JsonObject, { itemIndex: i });

// Never: throw new Error() — loses node context
```

---

## 11. Local dev & publishing

### Link for development

```bash
npm run build && npm link
mkdir -p ~/.n8n/custom && cd ~/.n8n/custom
npm init -y && npm link n8n-nodes-myapi
n8n start
```

Search in n8n editor by **display name**, not package name. Or set `N8N_CUSTOM_EXTENSIONS=/path/to/package`.

### Publishing checklist

```
[ ] name starts with n8n-nodes-
[ ] keywords includes "n8n-community-node-package"
[ ] license = "MIT", author + repository set
[ ] n8nNodesApiVersion = 1 (number!)
[ ] peerDependencies: { "n8n-workflow": "*" } — no runtime deps (for verified)
[ ] README.md in English with install/auth/usage
[ ] npm run lint passes, npm test passes
[ ] npm run build — icons land in dist/
[ ] Verified: publish via GitHub Actions with --provenance (required from May 2026)
[ ] Unverified: npm publish --access public
```

---

## 12. Key files to study

| Pattern | Source |
|---|---|
| Starter project | https://github.com/n8n-io/n8n-nodes-starter |
| Multi-resource versioned | `packages/nodes-base/nodes/HubSpot/V2/` |
| Polling trigger | `packages/nodes-base/nodes/Rss/RssFeedReadTrigger.node.ts` |
| Webhook lifecycle | `packages/nodes-base/nodes/Github/GithubTrigger.node.ts` |
| OAuth2 credentials | `packages/nodes-base/credentials/GoogleOAuth2Api.credentials.ts` |
| AI Tool sub-node | `packages/@n8n/nodes-langchain/nodes/tools/ToolHttpRequest/` |
| Vector Store factory | `packages/@n8n/nodes-langchain/nodes/vector_store/shared/` |

### Docs

- Overview: https://docs.n8n.io/integrations/creating-nodes/overview/
- Declarative: https://docs.n8n.io/integrations/creating-nodes/build/declarative-style-node/
- Programmatic: https://docs.n8n.io/integrations/creating-nodes/build/programmatic-style-node/
- Credentials: https://docs.n8n.io/integrations/creating-nodes/build/reference/credentials-files/
- Error handling: https://docs.n8n.io/integrations/creating-nodes/build/reference/error-handling/
- Submit community: https://docs.n8n.io/integrations/creating-nodes/deploy/submit-community-nodes/

---

## Top 10 gotchas

1. **`default` required on every property** — even `''` or `{}`
2. **Pass loop `i` to `getNodeParameter`** — not `0`
3. **Set `pairedItem`** on all output items via `constructExecutionMetaData`
4. **`noDataExpression: true`** on all resource/operation dropdowns
5. **`n8n-workflow` is peerDependency** — bundling it breaks `instanceof`
6. **`n8nNodesApiVersion: 1`** is a number, not a string
7. **Icons aren't compiled by tsc** — use `npm run build` (n8n-node build copies them)
8. **`this.helpers.request()` removed in v1** — use `httpRequest` or `httpRequestWithAuthentication`
9. **Don't advance polling cursor when `getMode() === 'manual'`**
10. **AI schemas must be Zod** — `DynamicStructuredTool` silently drops JSON Schema
