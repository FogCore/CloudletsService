mongo <<EOF
    use cloudlets_service
    db.cloudlets.createIndex({ip: 1}, {unique: true})
    db.createUser({user: 'cloudlets_service', pwd: 'cloudlets_service_pwd', roles: [ 'readWrite' ]})
EOF
