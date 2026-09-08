export default {
  extends: ['@commitlint/config-angular'],
  rules: {
    // Override: allow header up to 100 chars (Angular default is 100)
    'header-max-length': [2, 'always', 100],
    // Allow Chinese in subject (Angular config defaults to English-only)
    'subject-case': [0],
    // config-angular v19 dropped `chore` from type-enum; the project convention
    // keeps it for misc commits that touch neither source nor tests
    'type-enum': [
      2,
      'always',
      ['build', 'ci', 'docs', 'feat', 'fix', 'perf', 'refactor', 'revert', 'style', 'test', 'chore']
    ]
  }
};
