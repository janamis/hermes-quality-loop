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
  host,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

let rest = null
const KEY = ['quality-loop', 'campaigns']
const load = () => rest('/campaigns')
const call = (path, body) => rest(path, { method: 'POST', ...(body ? { body } : {}) })

function Field({ label, children }) {
  return jsxs('label', {
    className: 'flex min-w-0 flex-col gap-1',
    children: [
      jsx('span', { className: 'text-[0.6875rem] font-medium text-(--ui-text-tertiary)', children: label }),
      children
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
    className: 'flex flex-col gap-3 rounded-md border border-(--ui-stroke-secondary) p-3',
    children: [
      jsxs('div', {
        className: 'flex items-start justify-between gap-3',
        children: [
          jsxs('div', {
            className: 'min-w-0',
            children: [
              jsx('div', { className: 'truncate text-sm font-semibold', children: campaign.name }),
              jsx('div', {
                className: 'mt-0.5 text-[0.6875rem] text-(--ui-text-quaternary)',
                children: `${campaign.id} · board ${campaign.board} · profile ${campaign.assignee}`
              })
            ]
          }),
          jsx(Badge, { variant: statusVariant, children: campaign.state })
        ]
      }),
      jsxs('div', {
        className: 'grid grid-cols-2 gap-2 text-[0.75rem] sm:grid-cols-4',
        children: [
          jsx('div', { children: `Round ${campaign.round_no}/${campaign.max_rounds}` }),
          jsx('div', { children: `Stage: ${campaign.stage}` }),
          jsx('div', { children: `Repair ${campaign.repair_no}/${campaign.max_repairs}` }),
          jsx('div', { className: 'truncate', title: campaign.workspace, children: campaign.workspace })
        ]
      }),
      task
        ? jsxs('div', {
            className: 'rounded border border-(--ui-stroke-tertiary) px-2.5 py-2 text-[0.75rem]',
            children: [
              jsx('div', { className: 'font-medium', children: task.title }),
              jsx('div', {
                className: 'mt-0.5 text-(--ui-text-quaternary)',
                children: `${task.id} · ${task.status} · ${task.model || 'profile default'}`
              })
            ]
          })
        : null,
      campaign.message
        ? jsx('p', { className: 'text-[0.75rem] text-(--ui-text-tertiary)', children: campaign.message })
        : null,
      campaign.last_gate_result
        ? jsx('div', {
            className: 'text-[0.75rem] text-(--ui-text-tertiary)',
            children: `Hard gates: ${campaign.last_gate_result.ok ? 'passed' : 'failed'}${
              campaign.last_gate_result.commands?.length
                ? ` · ${campaign.last_gate_result.commands.map(item => `${item.name}:${item.exit_code ?? 'timeout'}`).join(' · ')}`
                : ''
            }`
          })
        : null,
      campaign.target_average != null
        ? jsx('div', {
            className: 'text-[0.75rem] text-(--ui-text-tertiary)',
            children: `Ranking: ${campaign.last_average == null ? 'not scored' : `${campaign.last_average}/10`} · target ${campaign.target_average}/10${campaign.publish_on_success ? ' · publish on final PASS' : ''}`
          })
        : null,
      jsxs('div', {
        className: 'flex flex-wrap gap-2',
        children: [
          jsx(Button, { size: 'xs', variant: 'outline', onClick: () => host.navigate('/kanban'), children: 'Open Kanban' }),
          jsx(Button, { size: 'xs', variant: 'ghost', disabled: act.isPending, onClick: action('reconcile'), children: 'Reconcile' }),
          campaign.state === 'running'
            ? jsx(Button, { size: 'xs', variant: 'ghost', disabled: act.isPending, onClick: action('pause'), children: 'Pause' })
            : null,
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
}

function CreateCampaign() {
  const qc = useQueryClient()
  const [name, setName] = useState('Codebase Quality Loop')
  const [board, setBoard] = useState('default')
  const [workspace, setWorkspace] = useState('')
  const [assignee, setAssignee] = useState('')
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
  const [publishOnSuccess, setPublishOnSuccess] = useState(false)
  const [publishRemote, setPublishRemote] = useState('origin')
  const [publishBranch, setPublishBranch] = useState('')
  const [commitMessage, setCommitMessage] = useState('quality-loop: reach target quality average')
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
        provider_override: provider.trim() || null,
        build_command: buildCommand,
        test_command: testCommand,
        gate_timeout_seconds: Number(gateTimeout),
        target_average: targetAverage === '' ? null : Number(targetAverage),
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
  const input = setter => event => setter(event.target.value)

  return jsxs('section', {
    className: 'flex flex-col gap-3 rounded-md border border-(--ui-stroke-secondary) p-3',
    children: [
      jsxs('div', {
        children: [
          jsx('h2', { className: 'text-sm font-semibold', children: 'New campaign' }),
          jsx('p', {
            className: 'mt-1 text-[0.75rem] text-(--ui-text-tertiary)',
            children: 'One Hermes profile, three per-card model overrides. The workspace must be an existing absolute project or worktree path.'
          })
        ]
      }),
      jsxs('div', {
        className: 'grid gap-3 md:grid-cols-2',
        children: [
          jsx(Field, { label: 'Campaign name', children: jsx(Input, { value: name, onChange: input(setName) }) }),
          jsx(Field, { label: 'Absolute workspace', children: jsx(Input, { value: workspace, onChange: input(setWorkspace), placeholder: '/absolute/path/to/your-repo' }) }),
          jsx(Field, { label: 'Kanban board', children: jsx(Input, { value: board, onChange: input(setBoard) }) }),
          jsx(Field, { label: 'Assignee profile', children: jsx(Input, { value: assignee, onChange: input(setAssignee) }) }),
          jsx(Field, { label: 'Examination model', children: jsx(Input, { value: examiner, onChange: input(setExaminer) }) }),
          jsx(Field, { label: 'Execution model', children: jsx(Input, { value: executor, onChange: input(setExecutor) }) }),
          jsx(Field, { label: 'Validation model', children: jsx(Input, { value: validator, onChange: input(setValidator) }) }),
          jsx(Field, { label: 'Provider override', children: jsx(Input, { value: provider, onChange: input(setProvider), placeholder: 'Optional, e.g. openai-codex' }) }),
          jsx(Field, { label: 'Target average (0–10)', children: jsx(Input, { type: 'number', min: 0.1, max: 10, value: targetAverage, onChange: input(setTargetAverage), placeholder: 'Optional; e.g. 9' }) }),
          jsx(Field, { label: 'Publish remote', children: jsx(Input, { value: publishRemote, onChange: input(setPublishRemote) }) }),
          jsx(Field, { label: 'Publish branch', children: jsx(Input, { value: publishBranch, onChange: input(setPublishBranch), placeholder: 'Current branch when blank' }) }),
          jsx(Field, { label: 'Commit message', children: jsx(Input, { value: commitMessage, onChange: input(setCommitMessage) }) }),
          jsx(Field, {
            label: 'Publish after final PASS',
            children: jsx(Input, { type: 'checkbox', checked: publishOnSuccess, onChange: event => setPublishOnSuccess(event.target.checked) })
          }),
          jsx(Field, {
            label: 'Build command (optional hard gate)',
            children: jsx(Input, { value: buildCommand, onChange: input(setBuildCommand), placeholder: 'npm run build' })
          }),
          jsx(Field, {
            label: 'Test command (optional if build is set)',
            children: jsx(Input, { value: testCommand, onChange: input(setTestCommand), placeholder: 'npm test' })
          }),
          jsxs('div', {
            className: 'grid grid-cols-3 gap-3 md:col-span-2',
            children: [
              jsx(Field, { label: 'Maximum rounds', children: jsx(Input, { type: 'number', min: 1, max: 100, value: rounds, onChange: input(setRounds) }) }),
              jsx(Field, { label: 'Repairs per change', children: jsx(Input, { type: 'number', min: 0, max: 20, value: repairs, onChange: input(setRepairs) }) }),
              jsx(Field, { label: 'Gate timeout (seconds)', children: jsx(Input, { type: 'number', min: 10, max: 3600, value: gateTimeout, onChange: input(setGateTimeout) }) })
            ]
          })
        ]
      }),
      jsx('div', {
        children: jsx(Button, {
          disabled: create.isPending || !workspace.trim() || !assignee.trim() || !examiner.trim() || !executor.trim() || !validator.trim() || (!buildCommand.trim() && !testCommand.trim()),
          onClick: () => create.mutate(),
          children: create.isPending ? 'Creating…' : 'Start campaign'
        })
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
  return jsxs('main', {
    className: 'flex h-full min-h-0 flex-col overflow-auto p-4',
    children: [
      jsxs('header', {
        className: 'mb-4 flex items-center justify-between gap-3',
        children: [
          jsxs('div', {
            children: [
              jsx('h1', { className: 'text-base font-semibold', children: 'Quality Loop' }),
              jsx('p', { className: 'text-[0.75rem] text-(--ui-text-tertiary)', children: 'Examine → execute → validate → repair/repeat → final audit' })
            ]
          }),
          jsx(Button, { size: 'sm', variant: 'outline', onClick: () => host.navigate('/kanban'), children: 'Kanban board' })
        ]
      }),
      jsxs('div', {
        className: 'flex max-w-5xl flex-col gap-4',
        children: [
          jsx(CreateCampaign, {}),
          campaigns.length
            ? jsxs('section', {
                className: 'flex flex-col gap-3',
                children: [
                  jsx('h2', { className: 'text-sm font-semibold', children: 'Campaigns' }),
                  ...campaigns.map(campaign => jsx(CampaignCard, { campaign }, campaign.id))
                ]
              })
            : jsx(EmptyState, { title: 'No campaigns yet', description: 'Enter an existing code workspace above to create the first Kanban examination card.' })
        ]
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
    })
  }
}
