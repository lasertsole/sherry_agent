/**
 * S0 shared components: render contracts for FormField, ListItemCard and
 * FilterBar. These are the reuse base for the S1-S6 UI upgrade plans; pinning
 * their props/slots/emits here keeps every downstream panel honest.
 */
import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import FormField from '../FormField.vue';
import ListItemCard from '../ListItemCard.vue';
import FilterBar from '../FilterBar.vue';

// PrimeVue auto-imports are inert custom elements under bare Vitest; stub them
// with the same component name so the FilterBar template binds to a real emit.
const stubs = {
  InputText: {
    name: 'InputText',
    props: ['modelValue', 'placeholder'],
    emits: ['update:modelValue'],
    template:
      '<input class="stub-input" :placeholder="placeholder" @input="$emit(\'update:modelValue\', $event.target.value)" />'
  },
  Button: {
    name: 'Button',
    emits: ['click'],
    template: '<button class="stub-button" @click="$emit(\'click\')">clear</button>'
  }
};

describe('FormField', () => {
  it('renders label, slotted control and hint', () => {
    const wrapper = mount(FormField, {
      props: { label: 'Name', hint: 'A hint' },
      slots: { default: '<input data-test="control" />' }
    });
    expect(wrapper.get('[data-test="form-field-label"]').text()).toBe('Name');
    expect(wrapper.find('[data-test="control"]').exists()).toBe(true);
    expect(wrapper.get('[data-test="form-field-hint"]').text()).toBe('A hint');
  });

  it('omits label and hint when not supplied', () => {
    const wrapper = mount(FormField);
    expect(wrapper.find('[data-test="form-field-label"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="form-field-hint"]').exists()).toBe(false);
  });
});

describe('ListItemCard', () => {
  it('renders title, badges, meta and emits click', async () => {
    const wrapper = mount(ListItemCard, {
      props: {
        title: 'frontend-expert',
        icon: 'pi pi-user',
        badges: [{ value: '2团', severity: 'info' }],
        meta: [{ icon: 'pi pi-cog', value: 'gpt-4' }]
      }
    });
    expect(wrapper.get('[data-test="list-item-title"]').text()).toBe('frontend-expert');
    expect(wrapper.get('[data-test="list-item-badge"]').text()).toBe('2团');
    expect(wrapper.get('[data-test="list-item-meta"]').text()).toContain('gpt-4');
    await wrapper.get('[data-test="list-item-card"]').trigger('click');
    expect(wrapper.emitted('click')).toHaveLength(1);
  });

  it('adds the active class when active', () => {
    const wrapper = mount(ListItemCard, { props: { title: 'x', active: true } });
    expect(wrapper.get('[data-test="list-item-card"]').classes()).toContain('border-theme-main');
  });
});

describe('FilterBar', () => {
  it('renders nothing when hidden', () => {
    expect(
      mount(FilterBar, { props: { show: false }, global: { stubs } })
        .find('[data-test="filter-bar"]')
        .exists()
    ).toBe(false);
  });

  it('emits keyword updates and clear', async () => {
    const wrapper = mount(FilterBar, {
      props: { show: true, keyword: '', keywordPlaceholder: 'Search', hasActiveFilters: true },
      global: { stubs }
    });
    expect(wrapper.get('.stub-input').attributes('placeholder')).toBe('Search');
    await wrapper.get('.stub-input').setValue('abc');
    expect(wrapper.emitted('update:keyword')?.at(-1)).toEqual(['abc']);
    await wrapper.get('.stub-button').trigger('click');
    expect(wrapper.emitted('clear')).toHaveLength(1);
  });

  it('hides the clear button when there are no active filters', () => {
    const wrapper = mount(FilterBar, { props: { show: true }, global: { stubs } });
    expect(wrapper.find('[data-test="filter-bar-clear"]').exists()).toBe(false);
  });
});
