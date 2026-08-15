<template>
  <NuxtLayout>
    <!-- 顶层 <NuxtPage> 不再包裹 keepalive：per-session 状态由 home/index.vue 内层
         <NuxtPage :page-key="route.params.sid"> 的 KeepAlive 按 page-key 缓存。
         若两层同时 keepalive（嵌套 KeepAlive），切换 session 时外层按路由名缓存
         home/index 单槽、内层按 page-key 换子页，二者节奏不一致会在首帧产生
         「新旧子页 DOM 短暂共存」的错位残影。移除外层冗余 keepalive 消除该竞态。 -->
    <NuxtPage/>
  </NuxtLayout>
  <ImagePreviewOverlay />
</template>

<script lang="ts" setup>

</script>

<style lang="scss" scoped>
  
</style>