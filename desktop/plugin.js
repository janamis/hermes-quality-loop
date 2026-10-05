import {
  Badge,
  Button,
  Codicon,
  EmptyState,
  ErrorState,
  Input,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Skeleton,
  Switch,
  host,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

let rest = null
let pluginOs = null
const KEY = ['quality-loop', 'campaigns']
const PROFILE_KEY = ['quality-loop', 'profiles']
const load = () => rest('/campaigns')
const call = (path, body) => rest(path, { method: 'POST', ...(body ? { body } : {}) })
const unique = values => [...new Set(values.filter(Boolean))]
const OFFLINE_MODEL_FALLBACKS = {
  'openai-codex': [
    'gpt-6.1-sol',
    'gpt-6.1-sol-900k',
    'gpt-6-astra',
    'gpt-6-astra-900k',
    'gpt-6-sol',
    'gpt-6-sol-900k',
    'gpt-6-luna',
    'gpt-6-luna-900k',
    'gpt-5.6-sol',
    'gpt-5.6-sol-900k',
    'gpt-5.6-terra',
    'gpt-5.6-terra-900k',
    'gpt-5.6-luna',
    'gpt-5.6-luna-900k',
    'gpt-5.5',
    'gpt-5.3-codex-spark'
  ],
  'custom:litellm': [
    'qwen3-coder:30b-a3b-q4_K_M',
    'laguna-xs-2.1:latest',
    'devstral-small-2:24b',
    'glm-4.7-flash:q4_K_M',
    'qwen3.8:27b',
    'qwen3.6:27b',
    'nemotron-cascade-2-30b-a3b:IQ4_XS',
    'qwen3.6:35b-a3b',
    'qwen3.5:9b',
    'qwen3.5:122b-a10b',
    'glm-5.3-flash:ud-iq2_xxs',
    'mistral-small4:119b-a6b-ud-q4_k_xl',
    'gpt-oss:120b',
    'glm-4.7-flash:q8_0',
    'glm-4.7-flash:bf16'
  ]
}

function Field({ label, hint, required = false, className = '', children }) {
  return jsxs('label', {
    className: `flex min-w-0 flex-col gap-1.5 ${className}`,
    children: [
      jsxs('span', {
        className: 'flex items-center gap-1 text-[0.6875rem] font-medium text-(--ui-text-secondary)',
        children: [label, required ? jsx('span', { className: 'text-(--ui-accent)', children: '•' }) : null]
      }),
      children,
      hint ? jsx('span', { className: 'text-[0.625rem] leading-4 text-(--ui-text-quaternary)', children: hint }) : null
    ]
  })
}

function SectionHeader({ step, icon, title, description }) {
  return jsxs('div', {
    className: 'flex items-start gap-3',
    children: [
      jsx('div', {
        className: 'grid size-8 shrink-0 place-items-center rounded-lg border border-(--ui-accent)/35 bg-(--ui-accent)/10 text-(--ui-accent)',
        children: jsx(Codicon, { name: icon, size: '0.9rem' })
      }),
      jsxs('div', {
        className: 'min-w-0',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('span', { className: 'text-[0.625rem] font-semibold uppercase tracking-[0.12em] text-(--ui-text-quaternary)', children: `Step ${step}` }),
              jsx('span', { className: 'h-px w-5 bg-(--ui-stroke-tertiary)' })
            ]
          }),
          jsx('h2', { className: 'mt-0.5 text-sm font-semibold text-(--ui-text-primary)', children: title }),
          jsx('p', { className: 'mt-0.5 text-[0.6875rem] leading-4 text-(--ui-text-tertiary)', children: description })
        ]
      })
    ]
  })
}

function PickerWithContent({ value, onChange, placeholder, disabled = false, items = [], ariaLabel, renderLabel }) {
  return jsxs(Select, {
    value,
    onValueChange: onChange,
    disabled,
    children: [
      jsx(SelectTrigger, {
        className: 'w-full',
        'aria-label': ariaLabel,
        children: jsx(SelectValue, { placeholder })
      }),
      jsx(SelectContent, {
        children: items.map(item => {
          const itemValue = typeof item === 'string' ? item : item.value
          const label = renderLabel ? renderLabel(item) : typeof item === 'string' ? item : item.label
          return jsx(SelectItem, { value: itemValue, children: label }, itemValue)
        })
      })
    ]
  })
}

function RoleModel({ icon, title, description, value, onChange, models, loading }) {
  return jsxs('div', {
    className: 'flex min-w-0 flex-col gap-3 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-bg-secondary)/35 p-3',
    children: [
      jsxs('div', {
        className: 'flex items-start gap-2.5',
        children: [
          jsx('div', {
            className: 'grid size-7 shrink-0 place-items-center rounded-md bg-(--ui-accent)/10 text-(--ui-accent)',
            children: jsx(Codicon, { name: icon, size: '0.8rem' })
          }),
          jsxs('div', {
            className: 'min-w-0',
            children: [
              jsx('div', { className: 'text-xs font-semibold text-(--ui-text-primary)', children: title }),
              jsx('p', { className: 'mt-0.5 text-[0.625rem] leading-4 text-(--ui-text-quaternary)', children: description })
            ]
          })
        ]
      }),
      loading
        ? jsx(Skeleton, { className: 'h-8 w-full' })
        : models.length
          ? jsx(PickerWithContent, {
              value,
              onChange,
              items: unique([value, ...models]),
              placeholder: 'Choose a model',
              ariaLabel: `${title} model`
            })
          : jsx(Input, {
              value,
              onChange: event => onChange(event.target.value),
              placeholder: 'Enter model ID',
              'aria-label': `${title} model`
            })
    ]
  })
}

function Stat({ label, value }) {
  return jsxs('div', {
    className: 'rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-secondary)/25 px-2.5 py-2',
    children: [
      jsx('div', { className: 'text-[0.5625rem] font-semibold uppercase tracking-wider text-(--ui-text-quaternary)', children: label }),
      jsx('div', { className: 'mt-0.5 truncate text-[0.75rem] font-medium text-(--ui-text-secondary)', title: String(value), children: value })
    ]
  })
}

function CampaignCard({ campaign }) {
  const qc = useQueryClient()
  const act = useMutation({
    mutationFn: action => call(`/campaigns/${campaign.id}/${action}`),
    onError: error => host.notifyError(error, 'Quality Loop action failed'),
    onSuccess: () => void qc.invalidateQueries({ queryKey: KEY })
  })
  const task = campaign.active_task
  const statusVariant = campaign.state === 'succeeded' ? 'success' : campaign.state === 'running' ? 'info' : 'secondary'
  const action = name => () => act.mutate(name)

  return jsxs('section', {
    className: 'overflow-hidden rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary)/15',
    children: [
      jsxs('div', {
        className: 'flex items-start justify-between gap-3 border-b border-(--ui-stroke-tertiary) px-4 py-3',
        children: [
          jsxs('div', {
            className: 'min-w-0',
            children: [
              jsx('div', { className: 'truncate text-sm font-semibold', children: campaign.name }),
              jsx('div', {
                className: 'mt-0.5 text-[0.625rem] text-(--ui-text-quaternary)',
                children: `${campaign.id} · ${campaign.assignee} · ${campaign.board}`
              })
            ]
          }),
          jsx(Badge, { variant: statusVariant, children: campaign.state })
        ]
      }),
      jsxs('div', {
        className: 'flex flex-col gap-3 p-4',
        children: [
          jsxs('div', {
            className: 'grid grid-cols-2 gap-2 sm:grid-cols-4',
            children: [
              jsx(Stat, { label: 'Round', value: `${campaign.round_no}/${campaign.max_rounds}` }),
              jsx(Stat, { label: 'Stage', value: campaign.stage }),
              jsx(Stat, { label: 'Repair', value: `${campaign.repair_no}/${campaign.max_repairs}` }),
              jsx(Stat, { label: 'Workspace', value: campaign.workspace })
            ]
          }),
          task
            ? jsxs('div', {
                className: 'flex items-start gap-2.5 rounded-md border border-(--ui-accent)/25 bg-(--ui-accent)/5 px-3 py-2.5 text-[0.75rem]',
                children: [
                  jsx(Codicon, { className: 'mt-0.5 shrink-0 text-(--ui-accent)', name: 'play-circle', size: '0.8rem' }),
                  jsxs('div', {
                    className: 'min-w-0',
                    children: [
                      jsx('div', { className: 'truncate font-medium', children: task.title }),
                      jsx('div', { className: 'mt-0.5 text-[0.625rem] text-(--ui-text-quaternary)', children: `${task.status} · ${task.model || 'profile default'}` })
                    ]
                  })
                ]
              })
            : null,
          campaign.message ? jsx('p', { className: 'text-[0.6875rem] leading-4 text-(--ui-text-tertiary)', children: campaign.message }) : null,
          campaign.last_gate_result
            ? jsx('div', {
                className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                children: `Hard gates: ${campaign.last_gate_result.ok ? 'passed' : 'failed'}${
                  campaign.last_gate_result.commands?.length
                    ? ` · ${campaign.last_gate_result.commands.map(item => `${item.name}:${item.exit_code ?? 'timeout'}`).join(' · ')}`
                    : ''
                }`
              })
            : null,
          campaign.target_average != null
            ? jsx('div', {
                className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                children: `Ranking: ${campaign.last_average == null ? 'not scored' : `${campaign.last_average}/10`} · target ${campaign.target_average}/10${campaign.publish_on_success ? ' · publish on final PASS' : ''}`
              })
            : null,
          campaign.prompt_profile === 'simple'
            ? jsx('div', {
                className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                children: 'Prompts: simple (local-model wording)'
              })
            : null,
          jsxs('div', {
            className: 'flex flex-wrap gap-2 pt-1',
            children: [
              jsx(Button, { size: 'xs', variant: 'outline', onClick: () => host.navigate('/kanban'), children: 'Open Kanban' }),
              jsx(Button, { size: 'xs', variant: 'ghost', disabled: act.isPending, onClick: action('reconcile'), children: 'Reconcile' }),
              campaign.state === 'running' ? jsx(Button, { size: 'xs', variant: 'ghost', disabled: act.isPending, onClick: action('pause'), children: 'Pause' }) : null,
              !['running', 'succeeded', 'stopped'].includes(campaign.state)
                ? jsx(Button, { size: 'xs', variant: 'outline', disabled: act.isPending, onClick: action('resume'), children: 'Resume' })
                : null,
              !['succeeded', 'stopped'].includes(campaign.state)
                ? jsx(Button, { size: 'xs', variant: 'ghost', disabled: act.isPending, onClick: action('stop'), children: 'Stop' })
                : null
            ]
          })
        ]
      })
    ]
  })
}

function CreateCampaign() {
  const qc = useQueryClient()
  const activeProfile = String(host.state.profile.get() || '')
  const [name, setName] = useState('Codebase Quality Loop')
  const [board, setBoard] = useState('default')
  const [workspace, setWorkspace] = useState('')
  const [workspaceBrowsing, setWorkspaceBrowsing] = useState(false)
  const [assignee, setAssignee] = useState(activeProfile)
  const [examiner, setExaminer] = useState('')
  const [executor, setExecutor] = useState('')
  const [validator, setValidator] = useState('')
  const [provider, setProvider] = useState('')
  const [buildCommand, setBuildCommand] = useState('')
  const [testCommand, setTestCommand] = useState('')
  const [gateTimeout, setGateTimeout] = useState('900')
  const [rounds, setRounds] = useState('20')
  const [repairs, setRepairs] = useState('3')
  const [targetAverage, setTargetAverage] = useState('')
  const [promptProfile, setPromptProfile] = useState('complete')
  const [publishOnSuccess, setPublishOnSuccess] = useState(false)
  const [publishRemote, setPublishRemote] = useState('origin')
  const [publishBranch, setPublishBranch] = useState('')
  const [commitMessage, setCommitMessage] = useState('quality-loop: reach target quality average')

  const profilesQuery = useQuery({
    queryKey: PROFILE_KEY,
    queryFn: () => host.request('profiles.list', { include_sessions: false }),
    staleTime: 30000
  })
  const profiles = profilesQuery.data?.profiles || []
  const selectedProfile = profiles.find(item => item.name === assignee) || null
  const profileProvider = String(selectedProfile?.provider || '')
  const profileModel = String(selectedProfile?.model || '')
  const modelsQuery = useQuery({
    queryKey: ['quality-loop', 'model-options-full-v10', assignee, activeProfile],
    queryFn: async () => {
      if (assignee === activeProfile) return host.request('model.options', { include_unconfigured: true })
      const routes = await host.profileRoutes()
      const route = routes.find(item => item.targetProfile === assignee) || routes.find(item => item.profile === assignee)
      if (!route) throw new Error(`No Hermes Desktop route is available for profile ${assignee}`)
      return host.requestProfile(route, 'model.options', { include_unconfigured: true })
    },
    enabled: Boolean(assignee),
    retry: false,
    staleTime: Infinity
  })
  const cachedModelsQuery = useQuery({
    queryKey: ['quality-loop', 'cached-model-options', assignee, profileProvider],
    queryFn: () => rest(`/model-catalog?profile=${encodeURIComponent(assignee)}&provider=${encodeURIComponent(profileProvider)}`),
    enabled: Boolean(assignee && profileProvider),
    retry: false,
    staleTime: Infinity
  })
  const catalog = modelsQuery.data || {}
  const cachedModels = unique((cachedModelsQuery.data?.models || []).map(String))
  const offlineModels = OFFLINE_MODEL_FALLBACKS[profileProvider] || []
  const providers = useMemo(() => {
    const rows = (catalog.providers || []).map(row => ({
      slug: String(row.slug || ''),
      label: String(row.name || row.label || row.slug || ''),
      models: unique((row.models || []).map(String))
    })).filter(row => row.slug)
    const currentSlug = String(catalog.provider || '')
    const currentModel = String(catalog.model || '')
    if (currentSlug && !rows.some(row => row.slug === currentSlug)) {
      rows.unshift({ slug: currentSlug, label: currentSlug, models: currentModel ? [currentModel] : [] })
    }
    if (profileProvider) {
      const row = rows.find(item => item.slug === profileProvider)
      if (row) row.models = unique([profileModel, ...cachedModels, ...row.models, ...offlineModels])
      if (!row) rows.unshift({ slug: profileProvider, label: profileProvider, models: unique([profileModel, ...cachedModels, ...offlineModels]) })
    }
    return rows
  }, [cachedModels, catalog, offlineModels, profileModel, profileProvider])
  const selectedProvider = providers.find(row => row.slug === provider)
  const models = selectedProvider?.models || []
  const configuredProvider = String(catalog.provider || profileProvider || '')
  const configuredModel = String(catalog.model || profileModel || '')

  useEffect(() => {
    if (!assignee && profiles.length) {
      const preferred = profiles.find(item => item.name === activeProfile) || profiles[0]
      setAssignee(preferred.name)
    }
  }, [activeProfile, assignee, profiles])

  useEffect(() => {
    if (modelsQuery.isFetching || provider) return
    const nextProvider = String(modelsQuery.data?.provider || profileProvider || providers[0]?.slug || '')
    if (nextProvider) setProvider(nextProvider)
  }, [modelsQuery.data, modelsQuery.isFetching, profileProvider, provider, providers])

  useEffect(() => {
    if (!provider || modelsQuery.isFetching) return
    const defaultModel = provider === configuredProvider && configuredModel
      ? configuredModel
      : models[0] || ''
    if (!examiner) setExaminer(defaultModel)
    if (!executor) setExecutor(defaultModel)
    if (!validator) setValidator(defaultModel)
  }, [configuredModel, configuredProvider, examiner, executor, models, modelsQuery.isFetching, provider, validator])

  const chooseProfile = next => {
    setAssignee(next)
    setProvider('')
    setExaminer('')
    setExecutor('')
    setValidator('')
  }
  const chooseProvider = next => {
    const row = providers.find(item => item.slug === next)
    const nextModel = next === configuredProvider && configuredModel ? configuredModel : row?.models?.[0] || ''
    setProvider(next)
    setExaminer(nextModel)
    setExecutor(nextModel)
    setValidator(nextModel)
  }
  const input = setter => event => setter(event.target.value)
  const browseWorkspace = async () => {
    if (!pluginOs) return
    setWorkspaceBrowsing(true)
    try {
      const selected = await pluginOs.pickOpenPath({
        title: 'Choose campaign workspace',
        defaultPath: workspace.trim() || String(host.state.cwd.get() || '') || undefined,
        directories: true
      })
      if (selected) setWorkspace(selected)
    } finally {
      setWorkspaceBrowsing(false)
    }
  }
  const providerOverride = provider && provider !== configuredProvider ? provider : null
  const missing = [
    !workspace.trim() ? 'workspace' : '',
    !assignee.trim() ? 'profile' : '',
    !examiner.trim() || !executor.trim() || !validator.trim() ? 'models' : ''
  ].filter(Boolean)

  const create = useMutation({
    mutationFn: () =>
      call('/campaigns', {
        name,
        board,
        workspace,
        assignee,
        examiner_model: examiner,
        executor_model: executor,
        validator_model: validator,
        provider_override: providerOverride,
        build_command: buildCommand,
        test_command: testCommand,
        gate_timeout_seconds: Number(gateTimeout),
        target_average: targetAverage === '' ? null : Number(targetAverage),
        prompt_profile: promptProfile,
        publish_on_success: publishOnSuccess,
        publish_remote: publishRemote,
        publish_branch: publishBranch.trim() || null,
        commit_message: commitMessage,
        max_rounds: Number(rounds),
        max_repairs: Number(repairs)
      }),
    onError: error => host.notifyError(error, 'Could not create Quality Loop campaign'),
    onSuccess: result => {
      host.notify({ kind: 'success', message: `Campaign ${result.campaign.id} created` })
      void qc.invalidateQueries({ queryKey: KEY })
    }
  })
  const canStart = !create.isPending && missing.length === 0

  return jsxs('section', {
    className: 'rounded-xl border border-(--ui-stroke-secondary) bg-(--ui-bg-primary)',
    children: [
      jsxs('div', {
        className: 'relative overflow-hidden border-b border-(--ui-stroke-secondary) bg-(--ui-accent)/5 px-5 py-4',
        children: [
          jsx('div', { className: 'pointer-events-none absolute -right-10 -top-16 size-40 rounded-full border border-(--ui-accent)/15 bg-(--ui-accent)/5' }),
          jsxs('div', {
            className: 'relative flex items-start justify-between gap-4',
            children: [
              jsxs('div', {
                className: 'flex items-start gap-3',
                children: [
                  jsx('div', {
                    className: 'grid size-10 shrink-0 place-items-center rounded-xl border border-(--ui-accent)/30 bg-(--ui-accent)/10 text-(--ui-accent)',
                    children: jsx(Codicon, { name: 'rocket', size: '1.05rem' })
                  }),
                  jsxs('div', {
                    children: [
                      jsx('h2', { className: 'text-[0.9375rem] font-semibold', children: 'Launch a quality campaign' }),
                      jsx('p', { className: 'mt-1 max-w-2xl text-[0.6875rem] leading-4 text-(--ui-text-tertiary)', children: 'Choose a Hermes profile, assemble a three-model review team, and set deterministic gates before any card is dispatched.' })
                    ]
                  })
                ]
              }),
              jsx(Badge, { variant: 'secondary', children: 'Kanban-backed' })
            ]
          }),
          jsxs('div', {
            className: 'relative mt-4 flex flex-wrap items-center gap-1.5 text-[0.625rem] text-(--ui-text-tertiary)',
            children: [
              jsx(Badge, { variant: 'outline', children: '1 · Examine' }),
              jsx(Codicon, { name: 'chevron-right', size: '0.6rem' }),
              jsx(Badge, { variant: 'outline', children: '2 · Execute' }),
              jsx(Codicon, { name: 'chevron-right', size: '0.6rem' }),
              jsx(Badge, { variant: 'outline', children: '3 · Validate' }),
              jsx(Codicon, { name: 'chevron-right', size: '0.6rem' }),
              jsx(Badge, { variant: 'outline', children: '4 · Final audit' })
            ]
          })
        ]
      }),
      jsxs('div', {
        className: 'flex flex-col gap-4 p-4',
        children: [
          jsxs('section', {
            className: 'rounded-lg border border-(--ui-stroke-tertiary) p-4',
            children: [
              jsx(SectionHeader, { step: '1', icon: 'folder-opened', title: 'Campaign setup', description: 'Name the run and point it at an existing repository or dedicated worktree.' }),
              jsxs('div', {
                className: 'mt-4 grid gap-3 md:grid-cols-2',
                children: [
                  jsx(Field, { label: 'Campaign name', required: true, children: jsx(Input, { value: name, onChange: input(setName) }) }),
                  jsx(Field, { label: 'Kanban board', required: true, hint: 'Cards and run history will be stored on this board.', children: jsx(Input, { value: board, onChange: input(setBoard) }) }),
                  jsx(Field, {
                    label: 'Absolute workspace',
                    required: true,
                    className: 'md:col-span-2',
                    hint: 'Use a clean project or worktree. Commands run with your Hermes permissions.',
                    children: jsxs('div', {
                      className: 'flex gap-2',
                      children: [
                        jsx(Input, { className: 'min-w-0 flex-1', value: workspace, onChange: input(setWorkspace), placeholder: '/absolute/path/to/your-repo' }),
                        jsx(Button, {
                          type: 'button',
                          size: 'sm',
                          variant: 'outline',
                          onClick: () => void browseWorkspace(),
                          disabled: workspaceBrowsing,
                          children: jsxs('span', {
                            className: 'inline-flex items-center gap-1.5',
                            children: [jsx(Codicon, { name: 'folder-opened', size: '0.75rem' }), workspaceBrowsing ? 'Browsing…' : 'Browse…']
                          })
                        }),
                        jsx(Button, { type: 'button', size: 'sm', variant: 'outline', onClick: () => setWorkspace(String(host.state.cwd.get() || '')), disabled: !host.state.cwd.get(), children: 'Use current' })
                      ]
                    })
                  }),
                  jsx(Field, {
                    label: 'Hermes assignee profile',
                    required: true,
                    className: 'md:col-span-2',
                    hint: profilesQuery.isError ? 'Profile discovery failed; reload the plugin or check the gateway.' : 'The selected profile supplies credentials, tools, and the model catalog for every campaign card.',
                    children: profilesQuery.isLoading
                      ? jsx(Skeleton, { className: 'h-8 w-full' })
                      : profiles.length
                        ? jsx(PickerWithContent, {
                            value: assignee,
                            onChange: chooseProfile,
                            items: profiles.map(item => ({ value: item.name, label: `${item.name}${item.is_default ? ' · default' : ''}${item.provider ? ` · ${item.provider}` : ''}` })),
                            placeholder: 'Choose a Hermes profile',
                            ariaLabel: 'Hermes assignee profile'
                          })
                        : jsx(Input, { value: assignee, onChange: input(setAssignee), placeholder: 'Hermes profile name', 'aria-label': 'Hermes assignee profile' })
                  })
                ]
              })
            ]
          }),
          jsxs('section', {
            className: 'rounded-lg border border-(--ui-stroke-tertiary) p-4',
            children: [
              jsx(SectionHeader, { step: '2', icon: 'organization', title: 'AI review team', description: 'One shared provider, with an independent model selected for each stage.' }),
              jsx('div', {
                className: 'mt-4',
                children: jsx(Field, {
                  label: 'Model provider',
                  hint: modelsQuery.isLoading
                    ? 'Loading the selected profile’s model catalog…'
                    : models.length
                      ? `${models.length} models available from ${provider}.${provider === configuredProvider ? ' This is the profile’s default provider.' : ' This override applies to all three roles.'}`
                      : provider === configuredProvider
                        ? 'Using the selected profile’s default provider.'
                        : 'This provider override applies to all three role models.',
                  children: modelsQuery.isLoading
                    ? jsx(Skeleton, { className: 'h-8 w-full' })
                    : providers.length
                      ? jsx(PickerWithContent, {
                          value: provider,
                          onChange: chooseProvider,
                          items: providers.map(item => ({ value: item.slug, label: `${item.label}${item.label === item.slug ? '' : ` · ${item.slug}`}` })),
                          placeholder: 'Choose a provider',
                          ariaLabel: 'Model provider'
                        })
                      : jsx(Input, { value: provider, onChange: input(setProvider), placeholder: 'Provider ID', 'aria-label': 'Model provider' })
                })
              }),
              jsxs('div', {
                className: 'mt-3 grid gap-3 lg:grid-cols-3',
                children: [
                  jsx(RoleModel, { icon: 'search', title: 'Examiner', description: 'Audits the codebase and prioritizes one improvement.', value: examiner, onChange: setExaminer, models, loading: modelsQuery.isLoading }),
                  jsx(RoleModel, { icon: 'tools', title: 'Executor', description: 'Implements the selected change in the workspace.', value: executor, onChange: setExecutor, models, loading: modelsQuery.isLoading }),
                  jsx(RoleModel, { icon: 'verified', title: 'Validator', description: 'Checks the result independently and decides PASS or FAIL.', value: validator, onChange: setValidator, models, loading: modelsQuery.isLoading })
                ]
              }),
              modelsQuery.isError && models.length <= 1
                ? jsx('div', {
                    className: 'mt-3 rounded-md border border-(--ui-stroke-tertiary) px-3 py-2 text-[0.6875rem] text-(--ui-text-tertiary)',
                    children: `The full model catalog could not be loaded (${String(modelsQuery.error)}). The profile’s configured model remains available.`
                  })
                : null,
              cachedModelsQuery.isError && models.length <= 1
                ? jsx('div', {
                    className: 'mt-3 rounded-md border border-(--ui-stroke-tertiary) px-3 py-2 text-[0.6875rem] text-(--ui-text-tertiary)',
                    children: `The cached model catalog could not be loaded (${String(cachedModelsQuery.error)}).`
                  })
                : null
            ]
          }),
          jsxs('section', {
            className: 'rounded-lg border border-(--ui-stroke-tertiary) p-4',
            children: [
              jsx(SectionHeader, { step: '3', icon: 'shield', title: 'Quality gates', description: 'Leave both commands empty to start with trusted project discovery.' }),
              jsxs('div', {
                className: 'mt-4 grid gap-3 md:grid-cols-2',
                children: [
                  jsx(Field, { label: 'Build command', hint: 'Optional override; leave both gates empty for discovery.', children: jsx(Input, { value: buildCommand, onChange: input(setBuildCommand), placeholder: 'Auto-discover' }) }),
                  jsx(Field, { label: 'Test command', hint: 'Optional override; discovery selects only trusted candidates.', children: jsx(Input, { value: testCommand, onChange: input(setTestCommand), placeholder: 'Auto-discover' }) }),
                  jsxs('div', {
                    className: 'grid grid-cols-3 gap-3 md:col-span-2',
                    children: [
                      jsx(Field, { label: 'Max rounds', children: jsx(Input, { type: 'number', min: 1, max: 100, value: rounds, onChange: input(setRounds) }) }),
                      jsx(Field, { label: 'Repairs/change', hint: 'Discovery chooses a bounded value when gates are automatic.', children: jsx(Input, { type: 'number', min: 0, max: 20, value: repairs, onChange: input(setRepairs) }) }),
                      jsx(Field, { label: 'Timeout (seconds)', children: jsx(Input, { type: 'number', min: 10, max: 3600, value: gateTimeout, onChange: input(setGateTimeout) }) })
                    ]
                  }),
                  jsx(Field, {
                    label: 'Prompt style',
                    className: 'md:col-span-2',
                    hint: 'Simple writes short stage instructions for small local models; Complete keeps the full guidance for cloud models.',
                    children: jsx(PickerWithContent, {
                      value: promptProfile,
                      onChange: setPromptProfile,
                      items: [
                        { value: 'complete', label: 'Complete · detailed prompts for cloud models' },
                        { value: 'simple', label: 'Simple · short prompts for local models' }
                      ],
                      placeholder: 'Choose prompt style',
                      ariaLabel: 'Prompt style'
                    })
                  }),
                  jsx(Field, { label: 'Target score (0–10)', className: 'md:col-span-2', hint: 'Optional. When set, the loop continues until this average and a final PASS are reached.', children: jsx(Input, { type: 'number', min: 0.1, max: 10, value: targetAverage, onChange: input(setTargetAverage), placeholder: 'Optional; e.g. 9' }) })
                ]
              })
            ]
          }),
          jsxs('section', {
            className: 'rounded-lg border border-(--ui-stroke-tertiary) p-4',
            children: [
              jsx(SectionHeader, { step: '4', icon: 'repo-push', title: 'Publication', description: 'Keep results local by default, or explicitly publish only after the final PASS.' }),
              jsxs('div', {
                className: 'mt-4 flex items-center justify-between gap-4 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-bg-secondary)/30 px-3 py-3',
                children: [
                  jsxs('div', {
                    children: [
                      jsx('div', { className: 'text-xs font-medium', children: 'Commit and push after final PASS' }),
                      jsx('div', { className: 'mt-0.5 text-[0.625rem] text-(--ui-text-quaternary)', children: 'Never merges or deploys. Requires a ranking target and a safe Git worktree.' })
                    ]
                  }),
                  jsx(Switch, { checked: publishOnSuccess, onCheckedChange: setPublishOnSuccess, 'aria-label': 'Publish after final pass' })
                ]
              }),
              publishOnSuccess
                ? jsxs('div', {
                    className: 'mt-3 grid gap-3 rounded-lg border border-(--ui-accent)/20 bg-(--ui-accent)/5 p-3 md:grid-cols-2',
                    children: [
                      jsx(Field, { label: 'Git remote', children: jsx(Input, { value: publishRemote, onChange: input(setPublishRemote) }) }),
                      jsx(Field, { label: 'Branch', children: jsx(Input, { value: publishBranch, onChange: input(setPublishBranch), placeholder: 'Current branch when blank' }) }),
                      jsx(Field, { label: 'Commit message', className: 'md:col-span-2', children: jsx(Input, { value: commitMessage, onChange: input(setCommitMessage) }) })
                    ]
                  })
                : null
            ]
          })
        ]
      }),
      jsxs('div', {
        className: 'sticky bottom-0 flex flex-wrap items-center justify-between gap-3 border-t border-(--ui-stroke-secondary) bg-(--ui-bg-primary)/95 px-5 py-3 backdrop-blur',
        children: [
          jsxs('div', {
            className: 'min-w-0',
            children: [
              jsx('div', { className: 'text-[0.6875rem] font-medium text-(--ui-text-secondary)', children: canStart ? 'Ready to create the first examination card.' : `Still needed: ${missing.join(', ')}.` }),
              jsx('div', { className: 'mt-0.5 truncate text-[0.625rem] text-(--ui-text-quaternary)', children: assignee ? `${assignee} · ${provider || 'loading provider'} · ${rounds} rounds max` : 'Choose a Hermes profile to load its model catalog.' })
            ]
          }),
          jsx(Button, {
            disabled: !canStart,
            onClick: () => create.mutate(),
            children: create.isPending
              ? 'Creating campaign…'
              : jsxs('span', { className: 'inline-flex items-center gap-1.5', children: [jsx(Codicon, { name: 'rocket', size: '0.75rem' }), 'Start campaign'] })
          })
        ]
      })
    ]
  })
}

function QualityLoopPage() {
  const query = useQuery({ queryKey: KEY, queryFn: load, refetchInterval: 5000 })
  if (query.isError) {
    return jsx('div', { className: 'p-4', children: jsx(ErrorState, { title: 'Quality Loop backend unavailable', description: String(query.error) }) })
  }
  const campaigns = query.data?.campaigns || []
  const active = campaigns.filter(campaign => campaign.state === 'running').length
  return jsxs('main', {
    className: 'h-full min-h-0 overflow-auto',
    children: [
      jsx('div', {
        className: 'mx-auto flex w-full max-w-6xl flex-col gap-5 p-5 lg:p-6',
        children: jsxs('div', {
          className: 'flex flex-col gap-5',
          children: [
            jsxs('header', {
              className: 'flex flex-wrap items-start justify-between gap-4',
              children: [
                jsxs('div', {
                  className: 'flex items-start gap-3',
                  children: [
                    jsx('div', { className: 'grid size-9 place-items-center rounded-lg bg-(--ui-accent)/10 text-(--ui-accent)', children: jsx(Codicon, { name: 'sync', size: '1rem' }) }),
                    jsxs('div', {
                      children: [
                        jsxs('div', { className: 'flex items-center gap-2', children: [jsx('h1', { className: 'text-lg font-semibold tracking-tight', children: 'Quality Loop' }), active ? jsx(Badge, { variant: 'info', children: `${active} active` }) : null] }),
                        jsx('p', { className: 'mt-1 text-[0.75rem] text-(--ui-text-tertiary)', children: 'Autonomous improvement campaigns with independent examination, execution, and validation.' })
                      ]
                    })
                  ]
                }),
                jsx(Button, { size: 'sm', variant: 'outline', onClick: () => host.navigate('/kanban'), children: jsxs('span', { className: 'inline-flex items-center gap-1.5', children: [jsx(Codicon, { name: 'project', size: '0.75rem' }), 'Open Kanban'] }) })
              ]
            }),
            jsx(CreateCampaign, {}),
            campaigns.length
              ? jsxs('section', {
                  className: 'flex flex-col gap-3',
                  children: [
                    jsxs('div', {
                      className: 'flex items-center justify-between gap-3',
                      children: [
                        jsxs('div', { children: [jsx('h2', { className: 'text-sm font-semibold', children: 'Campaigns' }), jsx('p', { className: 'mt-0.5 text-[0.6875rem] text-(--ui-text-quaternary)', children: 'Live state from the shared Quality Loop controller.' })] }),
                        jsx(Badge, { variant: 'secondary', children: String(campaigns.length) })
                      ]
                    }),
                    ...campaigns.map(campaign => jsx(CampaignCard, { campaign }, campaign.id))
                  ]
                })
              : jsx(EmptyState, { title: 'No campaigns yet', description: 'Complete the guided setup above to create the first Kanban examination card.' })
          ]
        })
      })
    ]
  })
}

function StatusCount() {
  const query = useQuery({ queryKey: KEY, queryFn: load, refetchInterval: 15000 })
  const active = (query.data?.campaigns || []).filter(campaign => campaign.state === 'running').length
  if (!active) return null
  return jsxs('button', {
    type: 'button',
    className: 'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
    onClick: () => host.navigate('/quality-loop'),
    children: [jsx(Codicon, { name: 'sync', size: '0.7rem' }), String(active)]
  })
}

export default {
  id: 'quality-loop',
  name: 'Quality Loop',
  description: 'Autonomous Kanban code examination, execution, validation, repair, and repeat campaigns.',
  defaultEnabled: false,
  register(ctx) {
    rest = ctx.rest
    pluginOs = ctx.os
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/quality-loop' }, render: () => jsx(QualityLoopPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 55, data: { path: '/quality-loop', label: 'Quality Loop', codicon: 'sync' } },
      { id: 'status', area: STATUSBAR_AREAS.right, order: 82, render: () => jsx(StatusCount, {}) },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: { id: 'quality-loop.open', label: 'Quality Loop: Open campaigns', keywords: ['kanban', 'quality', 'code', 'loop'], run: () => host.navigate('/quality-loop') }
      }
    ])
    ctx.onDispose(() => {
      rest = null
      pluginOs = null
    })
  }
}
