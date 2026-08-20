/**
 * @see https://prettier.io/docs/en/configuration.html
 * @type {import("prettier").Config}
 */
export default {
  // 一行最多 120 字符
  printWidth: 120,
  // 使用 2 个空格缩进
  tabWidth: 2,
  // 使用单引号
  singleQuote: true,
  // 如果希望使用分号，应设置："semi": true，不尾随就设置为 false
  semi: true,
  // 多行逗号分割的语法中，最后一行不加逗号
  trailingComma: 'none',
  jsxSingleQuote: true,
  // 单个参数的箭头函数不加括号 x => x
  arrowParens: 'avoid',
  // 对象大括号内两边是否加空格 { a:0 }
  bracketSpacing: true,
  // 将多行 HTML（HTML、JSX、Vue、Angular）元素的 > 放在最后一行的末尾，而不是单独一行。默认值为 false。
  bracketSameLine: true,
  endOfLine: 'auto',
  // 每行单个属性
  singleAttributePerLine: true
};
