// 在已登录的 ESXi Host Client 页面按 F12，粘贴整段脚本并回车。
// 只需修改 NAME、CPUS、MEMORY_MB。新机器先用 DHCP，不继承 192.168.10.50。
// 母盘必须保持关机；所有磁盘都是独立复制，不与母盘共享写入。
(async () => {
  const NAME = 'alinux4-infra-02';
  const CPUS = 8;
  const MEMORY_MB = 24576;
  const START_AFTER_CREATION = false;
  const BASE_VM_ID = '39';
  const EXPECTED_BASE_NAME = 'tpl-alinux4-base-20261127';
  const DATASTORE = 'datastore1';

  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$/.test(NAME)) throw Error('名称只能使用字母、数字、下划线和连字符，最多 63 字符');
  if (!Number.isInteger(CPUS) || CPUS < 1 || !Number.isInteger(MEMORY_MB) || MEMORY_MB < 2048) throw Error('检查 CPU 和内存设置');
  const injector = angular.element(document.querySelector('[ng-app]')).injector();
  const vim = injector.get('VIMService');
  const make = (type, fields) => new (vim.type(type))(fields);
  const add = device => make('VirtualDeviceConfigSpec', {operation: make('VirtualDeviceConfigSpecOperation', {value:'add'}), device});
  const base = await injector.get('VMService').getVM(BASE_VM_ID, true);
  if (base.name !== EXPECTED_BASE_NAME || base.runtime.powerState.value !== 'poweredOff') throw Error('母盘名称不符或母盘没有关机');
  const disks = base.config.hardware._device.filter(d => d.capacityInKB);
  if (disks.length !== 2 || disks.some(d => d.backing.parent)) throw Error('母盘磁盘布局已变化，请先检查');
  const directory = '[' + DATASTORE + '] ' + NAME;
  await vim.makeDirectory(directory); // 目录存在会报错，避免覆盖已有虚拟机。

  async function wait(task, label) {
    for (let n = 0; n < 1800; n++) {
      const p = await vim.listenForFields(task, ['info.state', 'info.error', 'info.result']);
      const state = p.info.state.value;
      if (state === 'success') { console.log(label + '：完成'); return p.info.result; }
      if (state === 'error') throw Error(label + '：' + (p.info.error?.localizedMessage || '失败'));
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    throw Error(label + '：等待超时，请查看 ESXi 近期任务；不要重复提交');
  }

  for (const d of disks) {
    const target = directory + (d.unitNumber === 0 ? '/os.vmdk' : '/data.vmdk');
    const spec = make('FileBackedVirtualDiskSpec', {diskType:'thin', adapterType:'lsiLogic', capacityKb:d.capacityInKB});
    const task = await vim.method('copyVirtualDisk_Task')(vim.createMoRef('VirtualDiskManager','ha-vdiskmanager'), d.backing.fileName, null, target, null, spec, false);
    await wait(task, '复制 ' + (d.unitNumber === 0 ? '系统盘' : '数据盘'));
  }

  const deviceChange = [
    add(make('VirtualNVMEController', {key:-1000, busNumber:0})),
    add(make('VirtualAHCIController', {key:-1500, busNumber:0})),
    ...disks.map(d => add(make('VirtualDisk', {key:-2000-d.unitNumber, controllerKey:-1000, unitNumber:d.unitNumber, capacityInKB:d.capacityInKB,
      backing:make('VirtualDiskFlatVer2BackingInfo', {fileName:directory+(d.unitNumber===0?'/os.vmdk':'/data.vmdk'), diskMode:'persistent', thinProvisioned:true})}))),
    add(make('VirtualVmxnet3', {key:-4000, addressType:'generated', backing:make('VirtualEthernetCardNetworkBackingInfo', {deviceName:'VM Network'}),
      connectable:make('VirtualDeviceConnectInfo', {startConnected:true, connected:false, allowGuestControl:true})})),
    add(make('VirtualCdrom', {key:-16000, controllerKey:-1500, unitNumber:0, backing:make('VirtualCdromAtapiBackingInfo', {deviceName:'',useAutoDetect:true}),
      connectable:make('VirtualDeviceConnectInfo', {startConnected:false,connected:false,allowGuestControl:true})}))
  ];
  const spec = make('VirtualMachineConfigSpec', {
    name:NAME, guestId:base.config.guestId, version:base.config.version, numCPUs:CPUS, numCoresPerSocket:CPUS, memoryMB:MEMORY_MB, firmware:'efi',
    files:make('VirtualMachineFileInfo', {vmPathName:directory+'/'+NAME+'.vmx'}),
    bootOptions:make('VirtualMachineBootOptions', {efiSecureBootEnabled:false}),
    memoryAllocation:make('ResourceAllocationInfo', {reservation:Math.ceil(MEMORY_MB*0.8/4)*4,limit:-1}),
    annotation:'Created from '+EXPECTED_BASE_NAME+'; initially DHCP. Set hostname/static IP and change the inherited root console password after deployment.',
    deviceChange
  });
  const task = await vim.method('createVM_Task')(vim.createMoRef('Folder','ha-folder-vm'), spec, vim.createMoRef('ResourcePool','ha-root-pool'));
  const result = await wait(task, '创建虚拟机');
  if (START_AFTER_CREATION) await wait(await vim.powerOnVM(result.value), '开机');
  return {name:NAME, id:result.value, directory, reservationMB:spec.memoryAllocation.reservation,
    url:location.origin+'/ui/#/host/vms/'+result.value, next:'开机后从路由器 DHCP 列表或控制台 ip -br addr 查看地址，再配置主机名和静态 IP。'};
})()
