# 在Ubuntu 24上添加OpenVPN自定义路由

由于`Ubuntu 24`提供的图形界面添加自定义路由不生效，所以必须通过命令的方式。那个人比较中意的命令是`nmcli connection`。

## 配置步骤

1. 列出现有的VPN连接

    ```shell
    nmcli connection show|grep vpn
    ```
   返回的第一列就是`VPN`连接的名称，下面需要用到的，假设我这里的`VPN`连接名称是`miles-ovpn`。

2. 修改`VPN`配置，添加静态路由

    假设要配置的静态路由是`172.24.220.0/24`和`172.19.19.0/24`，则修改如下：
    ```shell
   nmcli connection modify "miles-ovpn" +ipv4.routes "172.24.220.0/24"
   nmcli connection modify "miles-ovpn" +ipv4.routes "172.19.19.0/24"
   nmcli connection modify "miles-ovpn" ipv4.never-default "yes"
    ```

3. 重新加载配置并连接`VPN`

    ```shell
    nmcli connection up "miles-ovpn"
    ```

4. 验证路由

    ```shell
    ip route
    ```
    上面的命令正常情况下会输出类似以下的类目：
    ```shell
    172.19.19.0/24 dev tun0 proto static scope link metric 50 
    172.24.220.0/24 dev tun0 proto static scope link metric 50
    ```
    * `172.19.19.0/24`表示所有发往`172.19.19.0/24` 网段的流量通过`VPN`。
    * `tun0` 表示流量通过`VPN`接口（通常是`tun0`，具体名称以实际为准）
